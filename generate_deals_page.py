import csv
import json
import html
import os
import random
import re
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

US_CENTRAL = ZoneInfo("America/Chicago")


ROOT = Path(__file__).parent
JSON_PATH = ROOT / "deals_today.json"
DOCS_DIR = ROOT / "docs"
OUTPUT_PATH = DOCS_DIR / "index.html"
SNACK_BUDDY_ROOT = ROOT.parent / "snack-buddy"
WALMART_PRICE_DIRS = [
    SNACK_BUDDY_ROOT / "walmart",
    SNACK_BUDDY_ROOT / "Walmart",
]
PRODUCTS_FEED = SNACK_BUDDY_ROOT / "products_clean_unified.csv"
HISTORY_MASTER = SNACK_BUDDY_ROOT / "Data" / "historical_master_prices.csv"
ITEM_DATABASE_XLSX = SNACK_BUDDY_ROOT / "Item Database.xlsx"
_BUNDLE_TITLE_RE = re.compile(r"^\(\s*(\d+)\s*packs?\s*\)", re.I)
# Flat bundle price for this many calendar days → everyday multipack saving, not a deal.
EVERYDAY_BUNDLE_STABLE_DAYS = 30
_BUNDLE_PRICE_MATCH_TOL = 0.01  # cents-level scrape jitter still counts as "same"
# Structural multipack: baseline within 2% of N × smaller listing's current price.
_BUNDLE_BASELINE_VS_MULTIPLE_PCT = 0.02
# Until Item DB baselines for Walmart online bundles are approved, keep them
# out of the deals feed (they still get bundle_n / per-unit math for a future
# "Cheaper in the bundle" badge — never a deal tier).
EXCLUDE_WALMART_BUNDLES_FROM_DEALS = True
_AISLE_DETAIL_RE = re.compile(r"(?:^|;)\s*aisle=(True|False)\b", re.I)
_AISLE_LABEL_DETAIL_RE = re.compile(r"(?:^|;)\s*aisle_label=([^;]*)", re.I)
_SAME_DAY_PICKUP_DETAIL_RE = re.compile(
    r"(?:^|;)\s*same_day_pickup=(True|False|None)\b", re.I
)
_PICKUP_TEXT_DETAIL_RE = re.compile(r"(?:^|;)\s*pickup_text=([^;]*)", re.I)
_PICKUP_LATER_DATE_RE = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}\b",
    re.I,
)
_PICKUP_SOON_RE = re.compile(
    r"(?:\bin\s+\d+\s*(?:hours?|hrs?|mins?|minutes?)\b|as soon as\s+\d)",
    re.I,
)

# Supabase (public URL + publishable key only — never a secret/service key).
SUPABASE_URL = "https://krqwprircvvcdtgdqmpi.supabase.co"
SUPABASE_ANON_KEY = "sb_publishable_jVSVInpL8qMGyJP0kOCZkw_3UD74m34"

def load_deals():
    if not JSON_PATH.exists():
        raise SystemExit(f"JSON deals file not found: {JSON_PATH}")
    with JSON_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def parse_verified_at(deal):
    ts = deal.get("verified_at") or ""
    try:
        # Most likely ISO 8601
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _latest_verified_at(deals):
    times = [t for t in (parse_verified_at(d) for d in deals) if t is not None]
    return max(times) if times else datetime.now(timezone.utc)


def _as_us_central(dt: datetime) -> datetime:
    """Display scrape timestamps in US Central (not UTC)."""
    if dt.tzinfo is None:
        # Naive values are treated as UTC (ISO scrapes / pipeline clock).
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(US_CENTRAL)


def active_catalog_stats(db_path: Path | None = None) -> tuple[int, int]:
    """
    Count ACTIVE Item Database rows (all stores, including probation) and
    distinct retailers among them. Returns (tracked_count, store_count).
    """
    path = db_path or ITEM_DATABASE_XLSX
    if not path.is_file():
        return 0, 0
    try:
        from openpyxl import load_workbook
    except ImportError:
        print("WARNING: openpyxl missing; proof-of-work counts default to 0", flush=True)
        return 0, 0

    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    try:
        headers = [str(h or "").strip() for h in next(rows)]
    except StopIteration:
        wb.close()
        return 0, 0
    idx = {h: i for i, h in enumerate(headers)}
    status_i = idx.get("Status")
    retailer_i = idx.get("Retailer")
    if status_i is None or retailer_i is None:
        wb.close()
        return 0, 0

    tracked = 0
    stores: set[str] = set()
    for row in rows:
        status = str(row[status_i] or "").strip().upper()
        if status != "ACTIVE":
            continue
        tracked += 1
        retailer = str(row[retailer_i] or "").strip()
        if retailer:
            stores.add(retailer)
    wb.close()
    return tracked, len(stores)


def proof_of_work_line(*, tracked_count: int, store_count: int, deal_count: int) -> str:
    """Hero line under the h1 — build-time copy only (no JS)."""
    tracked_s = f"{tracked_count:,}"
    if deal_count == 1:
        sale_bit = "1 is on sale today."
    else:
        sale_bit = f"{deal_count} are on sale today."
    return (
        f"We check {tracked_s} snacks at {store_count} stores every day. {sale_bit}"
    )


def get_last_updated_text(deals):
    # Example: December 07, 2025 at 11:20 AM (US Central)
    return _as_us_central(_latest_verified_at(deals)).strftime("%B %d, %Y at %I:%M %p")


def get_last_updated_short(deals):
    # Compact sticky-header stamp: Aug 14 · 09:50 AM (US Central)
    return _as_us_central(_latest_verified_at(deals)).strftime("%b %d · %I:%M %p")


def retailer_classes(retailer: str):
    """
    Returns (pill_class, button_class) based on retailer.
    """
    r = (retailer or "").lower()
    if "walmart" in r:
        return "retailer-pill retailer-walmart", "view-button view-walmart"
    if "kroger" in r:
        return "retailer-pill retailer-kroger", "view-button view-kroger"
    if "target" in r:
        return "retailer-pill retailer-target", "view-button view-target"
    if "meijer" in r:
        return "retailer-pill retailer-meijer", "view-button view-meijer"
    if "harris" in r and "teeter" in r:
        return "retailer-pill retailer-harris-teeter", "view-button view-harris-teeter"
    return "retailer-pill retailer-generic", "view-button view-generic"


def retailer_cta_label(retailer: str) -> str:
    """Site CTA label for 'View on {label}.com' buttons."""
    r = (retailer or "").lower()
    if "harris" in r and "teeter" in r:
        return "HarrisTeeter"
    return (retailer or "Retailer").strip()


def display_retailer_label(retailer: str) -> str:
    """Visitor-facing store name (Harris Teeter shows city)."""
    r = (retailer or "").strip()
    rl = r.lower()
    if "harris" in rl and "teeter" in rl:
        return "Harris Teeter · Baltimore"
    return r or "Store"


def normalize_retailer_value(raw: str) -> str:
    r = (raw or "").strip().lower()
    if "walmart" in r:
        return "walmart"
    if "kroger" in r:
        return "kroger"
    if "target" in r:
        return "target"
    if "meijer" in r:
        return "meijer"
    if "harris" in r and "teeter" in r:
        return "harris teeter"
    return r  # fallback

def normalise_availability(raw):
    """Legacy helper — prefer howto_buy_line for under-price copy."""
    text, css = howto_buy_line({"availability": raw})
    return text or "Check availability at store", css or "availability check"


def _norm_url_key(url: str) -> str:
    u = (url or "").strip().lower()
    return u.split("?", 1)[0].split("#", 1)[0].rstrip("/")


def _latest_walmart_prices_csv() -> Path | None:
    cands: list[Path] = []
    for d in WALMART_PRICE_DIRS:
        if d.exists():
            cands.extend(d.glob("walmart_prices_*.csv"))
    if not cands:
        return None
    uniq = {c.resolve(): c for c in cands}
    return max(uniq.values(), key=lambda p: p.stat().st_mtime)


def _parse_aisle_from_detail(detail: str) -> tuple[bool | None, str]:
    """Parse aisle=True/False and aisle_label from walmart scrape detail string."""
    s = str(detail or "")
    m = _AISLE_DETAIL_RE.search(s)
    aisle = None
    if m:
        aisle = m.group(1).lower() == "true"
    label = ""
    lm = _AISLE_LABEL_DETAIL_RE.search(s)
    if lm:
        label = (lm.group(1) or "").strip()
    return aisle, label


def _same_day_pickup_from_text(pickup_txt: str) -> bool | None:
    t = (pickup_txt or "").strip().lower()
    if not t:
        return None
    if "not available" in t or "out of stock" in t:
        return None
    if "tomorrow" in t or _PICKUP_LATER_DATE_RE.search(t):
        return False
    if "today" in t or _PICKUP_SOON_RE.search(t):
        return True
    return None


def _parse_same_day_pickup_from_detail(detail: str) -> bool | None:
    """Prefer same_day_pickup= from detail; else infer from pickup_text=."""
    s = str(detail or "")
    m = _SAME_DAY_PICKUP_DETAIL_RE.search(s)
    if m:
        val = m.group(1).lower()
        if val == "true":
            return True
        if val == "false":
            return False
        return None
    pm = _PICKUP_TEXT_DETAIL_RE.search(s)
    if pm:
        return _same_day_pickup_from_text(pm.group(1) or "")
    return None


def _truthy_csv_flag(val) -> bool | None:
    if val is None or str(val).strip() == "":
        return None
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    if s in ("1", "true", "yes", "y", "t"):
        return True
    if s in ("0", "false", "no", "n", "f"):
        return False
    return None


def load_walmart_buy_context() -> dict:
    """
    page_title by URL + today's priced singles by (brand, flavor, pack)
    + DB keys for Walmart brand/flavor/pack (products feed)
    + aisle / pickup / shipping from the latest scrape (detail + flags).
    """
    by_url: dict[str, dict] = {}
    by_bfp: dict[tuple[str, str, int], list[dict]] = {}
    db_keys: set[tuple[str, str, int]] = set()
    has_aisle_data = False

    if PRODUCTS_FEED.exists():
        try:
            with PRODUCTS_FEED.open("r", encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    if "walmart" not in (row.get("retailer") or "").lower():
                        continue
                    try:
                        pack = int(float(row.get("Pack_Size") or row.get("pack_size") or 0))
                    except (TypeError, ValueError):
                        continue
                    if pack <= 0:
                        continue
                    brand = (row.get("Brand") or row.get("brand") or "").strip().lower()
                    flavor = (
                        row.get("Description/Flavor")
                        or row.get("description/flavor")
                        or row.get("flavor")
                        or ""
                    ).strip().lower()
                    db_keys.add((brand, flavor, pack))
        except OSError:
            pass

    path = _latest_walmart_prices_csv()
    if not path:
        return {
            "by_url": by_url,
            "by_bfp": by_bfp,
            "db_keys": db_keys,
            "has_aisle_data": has_aisle_data,
        }

    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
    except OSError:
        return {
            "by_url": by_url,
            "by_bfp": by_bfp,
            "db_keys": db_keys,
            "has_aisle_data": has_aisle_data,
        }

    for row in rows:
        url = _norm_url_key(row.get("canonical_url") or row.get("url") or "")
        title = (row.get("page_title") or "").strip()
        detail = row.get("detail") or row.get("availability_detail") or ""
        aisle, aisle_label = _parse_aisle_from_detail(detail)
        if aisle is not None:
            has_aisle_data = True
        same_day_pickup = _parse_same_day_pickup_from_detail(detail)
        pickup = _truthy_csv_flag(row.get("pickup_available"))
        shipping = _truthy_csv_flag(row.get("shipping_available"))
        if url:
            by_url[url] = {
                "page_title": title,
                "brand": (row.get("brand") or "").strip(),
                "flavor": (row.get("flavor") or "").strip(),
                "pack_size": row.get("pack_size"),
                "price": row.get("price"),
                "availability": (row.get("availability") or "").strip().lower(),
                "aisle": aisle,
                "aisle_label": aisle_label,
                "same_day_pickup": same_day_pickup,
                "pickup_available": pickup,
                "shipping_available": shipping,
                "detail": detail,
            }
        if (row.get("status") or "").strip().lower() != "ok":
            continue
        try:
            pack = int(float(row.get("pack_size") or 0))
            price = float(row.get("price"))
        except (TypeError, ValueError):
            continue
        if pack <= 0 or price <= 0:
            continue
        key = (
            (row.get("brand") or "").strip().lower(),
            (row.get("flavor") or "").strip().lower(),
            pack,
        )
        by_bfp.setdefault(key, []).append(
            {
                "price": price,
                "availability": (row.get("availability") or "").strip().lower(),
            }
        )
        # Tracked Walmart scrape rows are DB-backed; allow single lookup even if
        # feed flavor-column naming differs slightly.
        db_keys.add(key)

    return {
        "by_url": by_url,
        "by_bfp": by_bfp,
        "db_keys": db_keys,
        "has_aisle_data": has_aisle_data,
    }


def resolve_page_title(deal: dict, walmart_ctx: dict | None) -> str:
    direct = (deal.get("page_title") or "").strip()
    if direct:
        return direct
    if not walmart_ctx:
        return ""
    retailer = (deal.get("retailer") or "").lower()
    if "walmart" not in retailer:
        return ""
    url = _norm_url_key(deal.get("retailer_url") or deal.get("canonical_url") or "")
    meta = (walmart_ctx.get("by_url") or {}).get(url) or {}
    return (meta.get("page_title") or "").strip()


def single_each_in_store_price(
    deal: dict, bundle_n: int, walmart_ctx: dict | None
) -> float | None:
    """If DB has same brand+flavor at pack=total/N, return today's single price."""
    if not walmart_ctx or bundle_n <= 0:
        return None
    try:
        total = int(float(deal.get("pack_count") or deal.get("pack_size") or 0))
    except (TypeError, ValueError):
        return None
    if total <= 0 or total % bundle_n != 0:
        return None
    single_pack = total // bundle_n
    brand = (deal.get("brand") or "").strip().lower()
    flavor = (deal.get("flavor") or "").strip().lower()
    key = (brand, flavor, single_pack)
    if key not in (walmart_ctx.get("db_keys") or set()):
        return None
    cands = (walmart_ctx.get("by_bfp") or {}).get(key) or []
    if not cands:
        return None
    # Prefer an in-store-capable price for the "each in store" note.
    preferred = [
        c
        for c in cands
        if c.get("availability") in ("in_store", "both", "")
    ] or cands
    try:
        return float(preferred[0]["price"])
    except (TypeError, ValueError, KeyError):
        return None


def _deal_pack_count(deal: dict) -> int | None:
    try:
        total = int(float(deal.get("pack_count") or deal.get("pack_size") or 0))
    except (TypeError, ValueError):
        return None
    return total if total > 0 else None


def _deal_baseline(deal: dict) -> float | None:
    for key in ("old_price", "baseline", "baseline_db"):
        try:
            val = float(deal.get(key))
        except (TypeError, ValueError):
            continue
        if val > 0:
            return val
    return None


def walmart_title_bundle_n(deal: dict, walmart_ctx: dict | None = None) -> int | None:
    """N from Walmart '(N pack) ...' page title, else None."""
    retailer_l = (deal.get("retailer") or "").strip().lower()
    if "walmart" not in retailer_l:
        return None
    title = (deal.get("page_title") or "").strip() or resolve_page_title(
        deal, walmart_ctx
    )
    m = _BUNDLE_TITLE_RE.match(title or "")
    if not m:
        return None
    try:
        n = int(m.group(1))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def walmart_structural_bundle_n(
    deal: dict, walmart_ctx: dict | None = None
) -> int | None:
    """
    Detect multipack when title has no '(N pack)' but pack size is an exact
    multiple of a smaller same brand+flavor listing at Walmart, and baseline is
    within 2% of N × that smaller listing's current price.
    Returns N (units of the smaller pack), preferring the largest N.
    """
    retailer_l = (deal.get("retailer") or "").strip().lower()
    if "walmart" not in retailer_l or not walmart_ctx:
        return None
    pack = _deal_pack_count(deal)
    baseline = _deal_baseline(deal)
    if not pack or pack <= 1 or baseline is None:
        return None
    brand = (deal.get("brand") or "").strip().lower()
    flavor = (deal.get("flavor") or "").strip().lower()
    by_bfp = walmart_ctx.get("by_bfp") or {}
    best_n = None
    for single_pack in range(1, pack):
        if pack % single_pack != 0:
            continue
        n = pack // single_pack
        if n < 2:
            continue
        cands = by_bfp.get((brand, flavor, single_pack)) or []
        if not cands:
            continue
        try:
            smaller_price = float(cands[0]["price"])
        except (TypeError, ValueError, KeyError):
            continue
        if smaller_price <= 0:
            continue
        expected = n * smaller_price
        if abs(baseline - expected) / expected <= _BUNDLE_BASELINE_VS_MULTIPLE_PCT:
            if best_n is None or n > best_n:
                best_n = n
    return best_n


def walmart_bundle_n(deal: dict, walmart_ctx: dict | None = None) -> int | None:
    """
    Online-bundle detection = page title '(N pack) ...' only.
    Structural pack multiples are normal retail boxes, not bundles.
    """
    return walmart_title_bundle_n(deal, walmart_ctx)


_WALMART_IP_SKU_RE = re.compile(r"/ip/[^/]+/(\d{6,})", re.I)


def walmart_listing_sku(deal: dict) -> str:
    """Prefer retailer_sku; else parse trailing id from Walmart /ip/.../SKU URL."""
    sku = (deal.get("retailer_sku") or deal.get("sku") or "").strip()
    if sku:
        return sku
    url = (deal.get("retailer_url") or deal.get("canonical_url") or "").strip()
    m = _WALMART_IP_SKU_RE.search(url)
    return m.group(1) if m else ""


def _price_same(a: float, b: float, tol: float = _BUNDLE_PRICE_MATCH_TOL) -> bool:
    return abs(float(a) - float(b)) <= tol


def load_walmart_price_history_by_sku(
    skus: set[str],
) -> dict[str, dict[str, float]]:
    """
    date(YYYY-MM-DD) → price for requested Walmart SKUs from historical master.
    Last write wins when a date appears more than once.
    """
    out: dict[str, dict[str, float]] = {s: {} for s in skus if s}
    if not out or not HISTORY_MASTER.exists():
        return out
    try:
        with HISTORY_MASTER.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if "walmart" not in (row.get("retailer") or "").lower():
                    continue
                sku = (row.get("retailer_sku") or "").strip()
                if sku not in out:
                    continue
                day = (row.get("date") or "").strip()[:10]
                if len(day) != 10:
                    continue
                try:
                    price = float(row.get("price"))
                except (TypeError, ValueError):
                    continue
                if price <= 0:
                    continue
                out[sku][day] = price
    except OSError:
        pass
    return out


def is_everyday_bundle_price(
    dates_to_price: dict[str, float], today_price: float
) -> bool:
    """
    True when today's bundle price has held for EVERYDAY_BUNDLE_STABLE_DAYS+
    calendar days (same price walking backward from the newest history day).

    A fresh drop below that long-held price yields a short streak → False →
    treat as a normal time-limited deal again.
    """
    if not dates_to_price:
        return False
    try:
        today_p = float(today_price)
    except (TypeError, ValueError):
        return False
    if today_p <= 0:
        return False

    days = sorted(dates_to_price.keys())
    streak_days: list[str] = []
    for day in reversed(days):
        try:
            p = float(dates_to_price[day])
        except (TypeError, ValueError):
            break
        if _price_same(p, today_p):
            streak_days.append(day)
        else:
            # Older / higher "usual" price — today's level is new → not everyday.
            break
    if len(streak_days) < 2:
        return False
    try:
        newest = datetime.fromisoformat(streak_days[0]).date()
        oldest = datetime.fromisoformat(streak_days[-1]).date()
    except ValueError:
        return False
    return (newest - oldest).days >= EVERYDAY_BUNDLE_STABLE_DAYS


def is_drop_below_usual_bundle_price(
    dates_to_price: dict[str, float], today_price: float
) -> bool:
    """
    True when today's price is below a prior price that itself held for
    EVERYDAY_BUNDLE_STABLE_DAYS+ calendar days (time-limited multipack sale).
    """
    if not dates_to_price:
        return False
    try:
        today_p = float(today_price)
    except (TypeError, ValueError):
        return False
    if today_p <= 0:
        return False

    days = sorted(dates_to_price.keys())
    i = len(days) - 1
    # Skip the current-price streak.
    while i >= 0:
        try:
            p = float(dates_to_price[days[i]])
        except (TypeError, ValueError):
            return False
        if _price_same(p, today_p):
            i -= 1
            continue
        break
    if i < 0:
        return False
    try:
        prior = float(dates_to_price[days[i]])
    except (TypeError, ValueError):
        return False
    if prior <= today_p + _BUNDLE_PRICE_MATCH_TOL:
        return False  # not a drop from a higher usual

    prior_days: list[str] = []
    while i >= 0:
        try:
            p = float(dates_to_price[days[i]])
        except (TypeError, ValueError):
            break
        if _price_same(p, prior):
            prior_days.append(days[i])
            i -= 1
        else:
            break
    if len(prior_days) < 2:
        return False
    try:
        newest = datetime.fromisoformat(prior_days[0]).date()
        oldest = datetime.fromisoformat(prior_days[-1]).date()
    except ValueError:
        return False
    return (newest - oldest).days >= EVERYDAY_BUNDLE_STABLE_DAYS


def annotate_everyday_bundles(
    deals: list[dict], walmart_ctx: dict | None = None
) -> list[dict]:
    """
    Mark Walmart online-bundle listings from page title '(N pack) ...' only.

    Sets:
      deal['bundle_n'] / deal['bundle_source'] / deal['is_walmart_bundle']
      deal['is_everyday_bundle']  (per-unit / future badge math; never a deal tier)

    Structural pack multiples are normal boxes and are not marked. Bundle
    listings must never count as a deal or tier. EXCLUDE_WALMART_BUNDLES_FROM_DEALS
    drops title bundles from the feed until/unless you turn it off.
    Does not change prices or baselines.
    """
    bundle_meta: list[tuple[dict, int, str, float]] = []
    skus: set[str] = set()
    for deal in deals:
        n = walmart_title_bundle_n(deal, walmart_ctx)
        if not n:
            deal["is_everyday_bundle"] = False
            deal["is_walmart_bundle"] = False
            deal["bundle_n"] = None
            deal["bundle_source"] = None
            continue
        deal["is_walmart_bundle"] = True
        deal["bundle_n"] = n
        deal["bundle_source"] = "title"
        try:
            price = float(deal.get("new_price"))
        except (TypeError, ValueError):
            deal["is_everyday_bundle"] = True  # still not a deal tier
            continue
        sku = walmart_listing_sku(deal)
        if sku:
            deal["retailer_sku"] = deal.get("retailer_sku") or sku
            skus.add(sku)
            bundle_meta.append((deal, n, sku, price))
        else:
            deal["is_everyday_bundle"] = True

    history = load_walmart_price_history_by_sku(skus)
    for deal, n, sku, price in bundle_meta:
        hist = history.get(sku) or {}
        # Keep everyday / drop-below detection for a future badge.
        deal["is_everyday_bundle"] = is_everyday_bundle_price(hist, price)
        if EXCLUDE_WALMART_BUNDLES_FROM_DEALS:
            deal["is_everyday_bundle"] = True
    return deals


def filter_walmart_bundles_from_deals(deals: list[dict]) -> list[dict]:
    """Drop Walmart online-bundle listings from the deals feed (pending baseline approval)."""
    if not EXCLUDE_WALMART_BUNDLES_FROM_DEALS:
        return deals
    kept: list[dict] = []
    dropped = 0
    for d in deals:
        if d.get("is_walmart_bundle") or d.get("bundle_n"):
            dropped += 1
            continue
        kept.append(d)
    if dropped:
        print(
            f"WALMART_BUNDLES: excluded {dropped} listing(s) from deals "
            f"(baselines pending approval)",
            flush=True,
        )
    return kept


def _truthy_flag(val) -> bool:
    if isinstance(val, bool):
        return val
    if val is None:
        return False
    return str(val).strip().lower() in ("1", "true", "yes", "y", "t")


def _card_or_clip_prefix(deal: dict) -> str | None:
    """
    Kroger-family promo disclosure. Clip takes precedence over card.
    Other retailers → None (leave their how-to-buy labels unchanged).
    """
    retailer_l = (deal.get("retailer") or "").strip().lower()
    is_kroger = "kroger" in retailer_l
    is_ht = "harris" in retailer_l
    if not (is_kroger or is_ht):
        return None
    if _truthy_flag(deal.get("requires_clip")):
        return "Clip digital coupon"
    if _truthy_flag(deal.get("requires_card")):
        if is_ht:
            return "VIC card price"
        return "Kroger Plus card price"
    return None


def parse_promo_end(val) -> datetime | None:
    """Parse promo_end ISO/date to aware datetime; None if missing/unparseable."""
    s = str(val or "").strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        parsed = datetime.fromisoformat(s)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def is_expired_promo(deal: dict, *, now: datetime | None = None) -> bool:
    """True when Kroger/HT promo_end is parseable and already past (Central)."""
    end = parse_promo_end(deal.get("promo_end"))
    if end is None:
        return False
    now = now or datetime.now(tz=US_CENTRAL)
    if now.tzinfo is None:
        now = now.replace(tzinfo=US_CENTRAL)
    return end.astimezone(US_CENTRAL) < now.astimezone(US_CENTRAL)


def format_promo_end_phrase(deal: dict) -> str | None:
    """
    Shopper-facing end date for Kroger/HT, e.g. 'ends Sat 11/21' (Central).
    None when missing/unparseable — leave the card unchanged.
    """
    retailer_l = (deal.get("retailer") or "").strip().lower()
    if "kroger" not in retailer_l and "harris" not in retailer_l:
        return None
    end = parse_promo_end(deal.get("promo_end"))
    if end is None:
        return None
    central = end.astimezone(US_CENTRAL)
    return f"ends {central.strftime('%a')} {central.month}/{central.day}"


def _with_card_clip_prefix(text: str, css: str, deal: dict) -> tuple[str, str]:
    """
    Prefix how-to-buy with card/clip (and optional promo end date) for Kroger/HT.
    Example: 'Kroger Plus card price · ends Sat 11/21 · in store & online'
    """
    prefix = _card_or_clip_prefix(deal)
    end_bit = format_promo_end_phrase(deal)
    if (not prefix and not end_bit) or not text:
        return text, css
    rest = text[0].lower() + text[1:] if text else text
    bits = [b for b in (prefix, end_bit, rest) if b]
    return " · ".join(bits), css


def _walmart_fulfillment_flags(
    deal: dict, walmart_ctx: dict | None
) -> tuple[bool | None, bool | None, bool | None, bool | None]:
    """
    Return (aisle, same_day_pickup, pickup, shipping) for a Walmart deal.
    Prefers scrape context (detail aisle= / same_day_pickup= + tile flags).
    """
    aisle = None
    same_day = None
    pickup = _truthy_csv_flag(deal.get("pickup_available"))
    shipping = _truthy_csv_flag(deal.get("shipping_available"))
    if walmart_ctx:
        url = _norm_url_key(deal.get("retailer_url") or deal.get("canonical_url") or "")
        meta = (walmart_ctx.get("by_url") or {}).get(url) or {}
        if meta.get("aisle") is not None:
            aisle = bool(meta.get("aisle"))
        if "same_day_pickup" in meta:
            same_day = meta.get("same_day_pickup")
        if meta.get("pickup_available") is not None:
            pickup = bool(meta.get("pickup_available"))
        if meta.get("shipping_available") is not None:
            shipping = bool(meta.get("shipping_available"))
    return aisle, same_day, pickup, shipping


def howto_buy_line(
    deal: dict,
    *,
    page_title: str = "",
    walmart_ctx: dict | None = None,
) -> tuple[str, str]:
    """
    Under-price how-to-buy copy from availability_norm (+ Walmart bundles / aisle).
    Returns (text, css_class). Empty text → omit the line.
    """
    title = (page_title or "").strip() or resolve_page_title(deal, walmart_ctx)
    retailer = (deal.get("retailer") or "").strip()
    retailer_l = retailer.lower()

    # Walmart online bundle = page title '(N pack) ...' only.
    # Always "Online bundle only" — never "In store & online". Per-unit
    # comparison is kept on the deal object for a future badge, not this line.
    if "walmart" in retailer_l:
        n = deal.get("bundle_n") or walmart_bundle_n(deal, walmart_ctx)
        if n:
            try:
                n_int = int(n)
            except (TypeError, ValueError):
                n_int = 0
            if n_int > 0:
                # Preserve each-in-store math for future "Cheaper in the bundle".
                each = single_each_in_store_price(deal, n_int, walmart_ctx)
                if each is not None:
                    deal["bundle_each_in_store"] = each
                return "Online bundle only", "availability online-only"

        # In-store = aisle found OR same-day pickup. Otherwise online labels.
        # If both signals missing → fall through to availability_norm fallback.
        aisle, same_day, pickup, shipping = _walmart_fulfillment_flags(
            deal, walmart_ctx
        )
        if walmart_ctx and walmart_ctx.get("has_aisle_data"):
            in_store = (aisle is True) or (same_day is True)
            signals_present = (aisle is True) or (same_day is not None)
            if in_store:
                return "In store & online", "availability both"
            if signals_present and not in_store:
                if pickup:
                    return (
                        "Order online · pickup or delivery",
                        "availability online-only",
                    )
                if shipping:
                    return (
                        "Online only · ships from Walmart",
                        "availability online-only",
                    )

    avail = (
        deal.get("availability")
        or deal.get("availability_norm")
        or ""
    )
    if not isinstance(avail, str):
        return _with_card_clip_prefix(
            "Check availability at store", "availability check", deal
        )
    val = avail.strip().lower()
    if val == "both":
        return _with_card_clip_prefix("In store & online", "availability both", deal)
    if val == "in_store":
        return _with_card_clip_prefix("In store only", "availability in-store", deal)
    if val == "online_only":
        # Walmart.com-sold online-only items keep a fixed label.
        if "walmart" in retailer_l:
            return "Online only · ships from Walmart", "availability online-only"
        ship_from = retailer or "store"
        return _with_card_clip_prefix(
            f"Online only · ships from {ship_from}",
            "availability online-only",
            deal,
        )
    if val in ("check_store_availability", "check", "unknown", ""):
        return _with_card_clip_prefix(
            "Check availability at store", "availability check", deal
        )
    # Unrecognized → never omit the how-to-buy line.
    return _with_card_clip_prefix(
        "Check availability at store", "availability check", deal
    )


def howto_buy_html(
    deal: dict,
    *,
    page_title: str = "",
    walmart_ctx: dict | None = None,
) -> str:
    text, css = howto_buy_line(deal, page_title=page_title, walmart_ctx=walmart_ctx)
    if not text:
        return ""
    return f"<div class='{css}'>{html.escape(text)}</div>"


def _norm_str(val) -> str:
    """Normalize string values for grouping."""
    return (val or "").strip()


def _norm_float_key(val):
    """Normalize numeric values for grouping keys (so 1.9 and 1.90 group together)."""
    try:
        if val is None:
            return None
        return round(float(val), 4)
    except Exception:
        return None


def extract_flavor_from_product_name(product_name: str) -> str:
    """
    Extract flavor from product name.
    Example: "Quest Protein Chips Chili Lime (8ct)" -> "Chili Lime"
    Example: "ALOHA Protein Bar Peanut Butter Chocolate (1ct)" -> "Peanut Butter Chocolate"
    
    Strategy: Remove pack size, then try to identify common product type words
    and extract everything after them as the flavor.
    """
    if not product_name:
        return ""
    
    import re
    # Remove pack size in parentheses at the end
    name_part = re.sub(r'\s*\([^)]+\)\s*$', '', product_name).strip()
    
    # Common product type keywords that typically come before flavor
    product_types = [
        "protein bar", "protein chips", "energy drink", "bar", "chips",
        "cookies", "crackers", "drink", "beverage", "snack"
    ]
    
    # Try to find product type and extract flavor after it
    name_lower = name_part.lower()
    for ptype in sorted(product_types, key=len, reverse=True):  # Try longer matches first
        if ptype in name_lower:
            idx = name_lower.find(ptype)
            # Get everything after the product type
            flavor_part = name_part[idx + len(ptype):].strip()
            if flavor_part:
                return flavor_part
    
    # If no product type found, assume the last 2-3 words are the flavor
    # (after removing brand which is typically first word)
    words = name_part.split()
    if len(words) >= 3:
        # Skip first word (brand) and take the rest
        return " ".join(words[1:])
    
    return name_part


def extract_brand_from_product_name(product_name: str) -> str:
    """
    Extract brand from product name (usually first word or two).
    Example: "Quest Protein Chips Chili Lime (8ct)" -> "Quest"
    Example: "ALOHA Protein Bar Peanut Butter Chocolate (1ct)" -> "ALOHA"
    """
    if not product_name:
        return ""
    parts = product_name.split()
    if len(parts) >= 1:
        return parts[0]
    return ""


def extract_base_product_name(product_name: str) -> str:
    """
    Extract base product name without flavor and pack size.
    Example: "Quest Protein Chips Chili Lime (8ct)" -> "Quest Protein Chips"
    Example: "ALOHA Protein Bar Peanut Butter Chocolate (1ct)" -> "ALOHA Protein Bar"
    """
    if not product_name:
        return ""
    
    import re
    # Remove pack size in parentheses
    name_part = re.sub(r'\s*\([^)]+\)\s*$', '', product_name).strip()
    
    # Common product type keywords
    product_types = [
        "protein bar", "protein chips", "energy drink", "bar", "chips",
        "cookies", "crackers", "drink", "beverage", "snack"
    ]
    
    # Try to find product type and extract base name up to that point
    name_lower = name_part.lower()
    for ptype in sorted(product_types, key=len, reverse=True):
        if ptype in name_lower:
            idx = name_lower.find(ptype)
            # Get everything up to and including the product type
            base_part = name_part[:idx + len(ptype)].strip()
            if base_part:
                return base_part
    
    # Fallback: return first 2-3 words (brand + product type)
    words = name_part.split()
    if len(words) >= 2:
        return " ".join(words[:2])
    return name_part


def make_item_id(brand, product_name, pack_size) -> str:
    """Stable cross-retailer id for My Items starring (must match build_last_sale_json)."""
    b = " ".join(str(brand or "").strip().lower().split())
    p = " ".join(str(product_name or "").strip().lower().split())
    try:
        pack = str(int(float(pack_size)))
    except (TypeError, ValueError):
        pack = "0"
    return f"{b}|{p}|{pack}"


def remove_pack_size_from_name(product_name: str) -> str:
    """
    Remove pack size indicators like (1ct), (8ct), etc. from product name.
    Example: "Quest Protein Chips (8ct)" -> "Quest Protein Chips"
    Example: "ALOHA Protein Bar (1ct)" -> "ALOHA Protein Bar"
    """
    if not product_name:
        return ""
    
    import re
    # Remove pack size in parentheses at the end (e.g., (1ct), (8ct), (12 pack), etc.)
    cleaned = re.sub(r'\s*\([^)]*(?:ct|pack|count|pk)[^)]*\)\s*$', '', product_name, flags=re.IGNORECASE).strip()
    return cleaned


def group_deals(rows: list[dict]) -> list[dict]:
    """
    Group individual SKU rows into "deal families" so that each card represents:
      - same retailer
      - same section (Food / Drinks)
      - same brand (extracted from product_name)
      - same base product_name (without flavor)
      - same pack_size (pack_count + pack_unit)
      - same price / baseline (old_price)

    Flavors that share this key are stacked into one group. Each flavor stores
    its name and retailer_url for clickable links.

    Returns grouped deals with flavor_data containing {name, url, image_url} per flavor.
    """
    groups: dict[tuple, dict] = {}

    for row in rows:
        retailer = _norm_str(row.get("retailer"))
        section = _norm_str(row.get("section"))
        product_name = _norm_str(row.get("product_name"))
        # Use brand from JSON if available, otherwise extract from product_name
        brand = _norm_str(row.get("brand")) or extract_brand_from_product_name(product_name)
        pack_count = row.get("pack_count")
        pack_unit = _norm_str(row.get("pack_unit"))
        pack_size = f"{pack_count}{pack_unit}" if pack_count and pack_unit else ""
        
        # Use old_price as baseline, new_price as price
        price_raw = row.get("new_price")
        baseline_raw = row.get("old_price")
        
        price_key = _norm_float_key(price_raw)
        baseline_key = _norm_float_key(baseline_raw)
        
        # Use product_name directly (it's now the base name without flavor from CSV)
        base_product = product_name
        
        key = (
            retailer.lower(),
            section.lower(),
            brand.lower(),
            base_product.lower(),
            pack_size.lower(),
            price_key,
            baseline_key,
        )

        if key not in groups:
            # Start a new group using a shallow copy of the row
            g = dict(row)
            g["brand"] = brand
            g["flavor_data"] = []  # List of {name, url, image_url} dicts
            g["group_size"] = 0
            groups[key] = g

        g = groups[key]
        if row.get("is_everyday_bundle"):
            g["is_everyday_bundle"] = True
        if g.get("bundle_n") is None and row.get("bundle_n") is not None:
            g["bundle_n"] = row.get("bundle_n")

        # Get flavor from JSON if available, otherwise try to extract from product_name
        flavor_name = _norm_str(row.get("flavor")) or extract_flavor_from_product_name(product_name)
        retailer_url = _norm_str(row.get("retailer_url") or row.get("canonical_url") or "")
        
        # Check if this flavor already exists in the group (shouldn't happen, but handle it)
        existing_flavor = next((f for f in g["flavor_data"] if f["name"].lower() == flavor_name.lower()), None)
        if not existing_flavor and flavor_name and retailer_url:
            g["flavor_data"].append({
                "name": flavor_name,
                "url": retailer_url,
                "image_url": _norm_str(row.get("image_url") or ""),
                "is_everyday_bundle": bool(row.get("is_everyday_bundle")),
                "bundle_n": row.get("bundle_n"),
            })
            g["group_size"] = len(g["flavor_data"])

    # Flavor images are shown in per-card carousel; no separate text expand list
    for g in groups.values():
        flavors = g.get("flavor_data") or []
        if flavors:
            flavors.sort(key=lambda x: x["name"].lower())
        g["flavor_sample"] = []
        g["flavor_extra_count"] = 0
        g["flavor_extra_data"] = []

    return list(groups.values())


def get_tier_name(percent_off, deal: dict | None = None):
    """
    Get tier name from percent_off for filtering.
    Returns: "diamond", "fire", "strong", "sale", or "bundle"
    Walmart bundles are "bundle" (never a deal-tier chip).
    """
    if deal and (deal.get("is_everyday_bundle") or deal.get("is_walmart_bundle") or deal.get("bundle_n")):
        return "bundle"
    try:
        pct = float(percent_off)
    except Exception:
        pct = 0.0

    if pct >= 25.0:
        return "diamond"
    if pct >= 20.0:
        return "fire"
    if pct >= 10.0:
        return "strong"
    if pct >= 0.0:
        return "sale"
    return "sale"  # fallback


# Vertically elongated 4-point sparkle with concave sides (quadratic curves).
# Tips: top/bottom long, left/right short (~2:1 axis ratio).
SPARKLE_STAR_PATH = (
    "M50 0 "
    "Q56 39 76 50 "
    "Q56 61 50 100 "
    "Q44 61 24 50 "
    "Q44 39 50 0 "
    "Z"
)


def sparkle_star_svg() -> str:
    return (
        f'<svg class="sparkle-svg" viewBox="0 0 100 100" aria-hidden="true" focusable="false">'
        f'<path class="sparkle-shape" d="{SPARKLE_STAR_PATH}"/></svg>'
    )


def fire_pill_flames_svg() -> str:
    """Cartoon flame strip for the bottom edge of the Fire badge pill."""
    return """
<svg class="fire-pill-flames-svg" viewBox="0 0 120 24" preserveAspectRatio="none" aria-hidden="true" focusable="false">
  <path class="fire-pill-flame-outer" fill="#fb923c" d="M0,24 L0,16 C5,9 10,17 15,12 C20,7 25,15 30,10 C35,6 40,14 45,9 C50,5 55,13 60,8 C65,4 70,12 75,9 C80,6 85,13 90,10 C95,7 100,14 105,11 C110,8 115,15 120,13 L120,24 Z"/>
  <path class="fire-pill-flame-inner" fill="#fef08a" d="M0,24 L0,18 C7,14 14,17 21,15 C28,13 35,17 42,14 C49,12 56,16 63,14 C70,12 77,16 84,14 C91,13 98,17 105,15 C112,14 116,17 120,16 L120,24 Z"/>
  <ellipse class="fire-pill-spark fire-pill-spark--1" cx="18" cy="8" rx="2" ry="3" fill="#fb923c"/>
  <ellipse class="fire-pill-spark fire-pill-spark--2" cx="58" cy="6" rx="1.6" ry="2.6" fill="#fb923c"/>
  <ellipse class="fire-pill-spark fire-pill-spark--3" cx="96" cy="9" rx="1.8" ry="2.8" fill="#fb923c"/>
</svg>""".strip()


def build_diamond_sparkles_html(seed_key: str) -> str:
    """Per-card randomized ✦ sparkles — present but not overbearing."""
    rng = random.Random(seed_key)
    count = rng.randint(6, 10)
    # Prefer calmer single/double flashes over rapid multi-flash
    twinkle_anims = (
        "diamond-twinkle-a",
        "diamond-twinkle-a",
        "diamond-twinkle-b",
        "diamond-twinkle-c",
        "diamond-twinkle-c",
        "diamond-twinkle-d",
    )
    timing_fns = ("linear",)

    def sparkle_size(base: int) -> tuple[int, int]:
        """Square canvas keeps the sparkle SVG symmetric."""
        return base, base

    stars = []
    for _ in range(count):
        width, height = sparkle_size(rng.randint(12, 26))

        top = rng.uniform(4, 88)
        if rng.random() < 0.5:
            horizontal = f"left:{rng.uniform(2, 86):.1f}%;"
        else:
            horizontal = f"right:{rng.uniform(2, 86):.1f}%;left:auto;"

        anim = rng.choice(twinkle_anims)
        duration = rng.uniform(5.5, 9.0)
        delay = rng.uniform(0, 6.0)
        peak = rng.uniform(0.8, 1.15)

        style = (
            f"width:{width:.0f}px;height:{height:.0f}px;"
            f"top:{top:.1f}%;{horizontal}"
            f"animation-name:{anim};"
            f"animation-duration:{duration:.2f}s;"
            f"animation-delay:{delay:.2f}s;"
            f"animation-timing-function:{rng.choice(timing_fns)};"
            f"--sparkle-peak:{peak:.2f};"
        )
        stars.append(
            f'<span class="sparkle" style="{style}">{sparkle_star_svg()}</span>'
        )

    body = "\n            ".join(stars)
    return f"""
        <div class="diamond-sparkles" aria-hidden="true">
            {body}
        </div>"""


def get_badge(deal):
    """
    Decide badge label & class from percent_off.
    Tier system:
    - everyday Walmart bundle → Cheaper in the bundle (badge-bundle)
    - ≥25% → 💎 Diamond (badge-elite)
    - 20–24.99% → 🔥 Fire (badge-strong)
    - 10–19.99% → 💪 Strong (badge-protein)
    - 0–9.99% → 🏷️ On Sale (badge-everyday)
    """
    # Future badge: per-unit bundle saving — never a deal tier.
    if deal.get("is_everyday_bundle") or deal.get("is_walmart_bundle") or deal.get("bundle_n"):
        return "Cheaper in the bundle", "badge badge-bundle"

    try:
        percent_off = float(deal.get("percent_off", 0.0))
    except Exception:
        percent_off = 0.0

    # Check for Diamond tier (≥25%)
    if percent_off >= 25.0:
        return "💎 Diamond", "badge badge-elite"
    # Check for Fire tier (20–24.99%)
    if percent_off >= 20.0:
        return "🔥 Fire", "badge badge-strong"
    # Check for Strong tier (10–19.99%)
    if percent_off >= 10.0:
        return "💪 Strong", "badge badge-protein"
    # Check for On Sale tier (0–9.99%)
    if percent_off >= 0.0:
        return "🏷️ On Sale", "badge badge-everyday"
    # No badge for negative percentages
    return "", ""


def format_streak(deal):
    streak = deal.get("streak_days")
    try:
        if streak is None:
            return ""
        streak_int = int(streak)
        if streak_int < 1:
            return ""
    except Exception:
        return ""
    return f"Day {streak_int}"


def get_card_image_items(deal) -> list[dict]:
    """All flavor images for the card carousel (falls back to deal image)."""
    flavor_data = deal.get("flavor_data") or []
    fallback_img = _norm_str(deal.get("image_url") or "")
    fallback_url = _norm_str(deal.get("retailer_url") or deal.get("canonical_url") or "#")
    items = []

    for f in flavor_data:
        img = _norm_str(f.get("image_url") or "") or fallback_img
        if not img:
            continue
        items.append({
            "name": _norm_str(f.get("name") or ""),
            "url": _norm_str(f.get("url") or "") or fallback_url,
            "image_url": img,
        })

    if not items and fallback_img:
        items.append({
            "name": "",
            "url": fallback_url,
            "image_url": fallback_img,
        })

    return items


def build_card_image_carousel_html(deal, default_alt: str) -> str:
    items = get_card_image_items(deal)
    if not items:
        return '<div class="card-carousel card-carousel--empty"></div>'

    slides = []
    for i, item in enumerate(items):
        img_url = html.escape(item["image_url"])
        alt = html.escape(item["name"] or default_alt)
        url = html.escape(item["url"] or "#")
        flavor_name = html.escape(item["name"])
        slides.append(
            f'<div class="card-carousel-slide" data-index="{i}" data-flavor-name="{flavor_name}">'
            f'<a href="{url}" target="_blank" rel="noopener noreferrer" class="card-carousel-link">'
            f'<img src="{img_url}" alt="{alt}" class="card-image" loading="lazy"/>'
            f"</a></div>"
        )

    multi = len(items) > 1
    arrows_html = ""
    if multi:
        arrows_html = """
            <button type="button" class="card-carousel-prev" aria-label="Previous flavor">&#8249;</button>
            <button type="button" class="card-carousel-next" aria-label="Next flavor">&#8250;</button>
        """

    first_caption = html.escape(items[0]["name"]) if items[0].get("name") else ""
    caption_html = ""
    if first_caption:
        caption_html = f'<div class="card-carousel-caption">{first_caption}</div>'

    single_class = "" if multi else " card-carousel--single"

    return f"""
        <div class="card-carousel{single_class}" data-slide-count="{len(items)}">
            <div class="card-carousel-stage">
                {"".join(slides)}
            </div>
            {arrows_html}
            {caption_html}
        </div>
    """


def build_card_html(deal, walmart_ctx: dict | None = None):
    # Use product_name directly from the deal (from CSV, not combined)
    # Remove pack size indicators since we have a pack pill overlay
    product_name = deal.get("product_name", "")
    product_name_cleaned = remove_pack_size_from_name(product_name)
    name = html.escape(product_name_cleaned)
    retailer = html.escape(deal.get("retailer", ""))
    old_price = deal.get("old_price")
    new_price = deal.get("new_price")
    percent_off = float(deal.get("percent_off", 0.0))
    retailer_url = html.escape(deal.get("retailer_url", "#"))
    image_carousel_html = build_card_image_carousel_html(deal, product_name_cleaned)
    
    # Get the number of flavors/deals in this group (default to 1 if no flavor_data)
    flavor_data = deal.get("flavor_data", [])
    deal_count = len(flavor_data) if flavor_data else 1
    
    flavor_extra_count = deal.get("flavor_extra_count", 0)
    flavor_extra_data = deal.get("flavor_extra_data", [])

    # Normalized values for filters (used in data-* attributes)
    section_raw = (deal.get("section") or "").strip().lower()
    category_raw = (deal.get("category") or "").strip().lower()
    retailer_raw = normalize_retailer_value(deal.get("retailer") or "")
    brand_raw = (_norm_str(deal.get("brand")) or extract_brand_from_product_name(product_name)).lower()
    pack_count_for_id = deal.get("pack_count")
    item_id = make_item_id(brand_raw, product_name, pack_count_for_id)
    item_id_attr = html.escape(item_id)

    section_attr = html.escape(section_raw)
    category_attr = html.escape(category_raw)
    retailer_attr = html.escape(retailer_raw)
    brand_attr = html.escape(brand_raw)

    # Retailer specific styling
    pill_class, button_class = retailer_classes(deal.get("retailer", ""))

    # How-to-buy line (under price)
    buy_html = howto_buy_html(deal, walmart_ctx=walmart_ctx)

    # Badge
    badge_label, badge_class = get_badge(deal)
    is_everyday_bundle = bool(
        deal.get("is_everyday_bundle")
        or deal.get("is_walmart_bundle")
        or deal.get("bundle_n")
    )

    # Tier for filtering (Walmart bundles are not deal tiers)
    tier_name = get_tier_name(percent_off, deal)
    tier_class = f"card-tier-{tier_name}"

    # Search blob: title + brand + flavors (client-side filter)
    search_parts = [
        product_name_cleaned or name,
        brand_raw,
        category_raw,
        retailer_raw,
    ]
    for f in flavor_data:
        search_parts.append(f.get("name") or "")
    for f in flavor_extra_data:
        search_parts.append(f.get("name") or "")
    search_attr = html.escape(
        " ".join(str(p).strip() for p in search_parts if str(p or "").strip()).lower()
    )

    # Streak
    streak_text = format_streak(deal)

    # Pack size pill
    pack_count = deal.get("pack_count")
    pack_unit = deal.get("pack_unit")
    pack_pill_html = ""
    try:
        if pack_count is not None and pack_unit:
            pack_pill_html = (
                f"<div class='pack-pill'>"
                f"<span class='pack-count'>{int(pack_count)}</span> "
                f"<span class='pack-unit'>{html.escape(pack_unit)}</span>"
                f"</div>"
            )
    except Exception:
        pack_pill_html = ""

    # Make sure pricing can be cast safely
    try:
        new_price_val = float(new_price) if new_price is not None else 0.0
    except Exception:
        new_price_val = 0.0

    old_price_val = None
    old_price_html = ""
    if old_price is not None:
        try:
            old_price_val = float(old_price)
            if old_price_val > new_price_val:
                old_price_html = (
                    f"<span class='old-price'>${old_price_val:.2f}</span>"
                )
            else:
                old_price_val = None
        except Exception:
            old_price_val = None
            old_price_html = ""

    dollars_saved = (
        (old_price_val - new_price_val) if old_price_val is not None else 0.0
    )
    if is_everyday_bundle:
        # Not a time-limited deal — don't imply baseline savings dollars/strike.
        old_price_html = ""
        dollars_saved = 0.0

    streak_html = ""
    if streak_text and not is_everyday_bundle:
        streak_html = f"<div class='streak'>{streak_text}</div>"

    badge_html = ""
    if badge_label and badge_class:
        if tier_name == "fire":
            import hashlib
            flicker_seed = f"{retailer_raw}|{name}|{new_price_val}"
            flicker_delay = (
                int(hashlib.md5(flicker_seed.encode()).hexdigest()[:4], 16) % 75
            ) / 10.0
            badge_html = (
                f"<div class='{badge_class} badge-fire-pill'>"
                f"<span class='fire-pill-flames' style='animation-delay:{flicker_delay:.1f}s' "
                f"aria-hidden='true'>{fire_pill_flames_svg()}</span>"
                f"<span class='fire-pill-label'>🔥 Fire</span></div>"
            )
        else:
            badge_html = f"<div class='{badge_class}'>{html.escape(badge_label)}</div>"

    percent_off_html = ""
    if not is_everyday_bundle:
        percent_off_html = (
            f"<span class='percent-off'>{percent_off:.0f}% OFF</span>"
        )

    diamond_sparkles_html = ""
    diamond_style_attr = ""
    if tier_name == "diamond":
        import hashlib
        sparkle_seed = f"{retailer_raw}|{name}|{new_price_val}|{deal_count}"
        diamond_sparkles_html = build_diamond_sparkles_html(sparkle_seed)
        # Unique sheen phase + duration per card so sweeps rarely align
        h = int(
            hashlib.md5(
                f"{retailer_raw}|{name}|{new_price_val}|{deal_count}|sheen".encode()
            ).hexdigest()[:8],
            16,
        )
        sheen_duration = 4.4 + (h % 23) / 10.0  # 4.4–6.6s, drift apart over time
        sheen_delay = ((h >> 8) % 1000) / 1000.0 * sheen_duration
        badge_delay = ((h >> 16) % 1000) / 1000.0 * 2.8
        diamond_style_attr = (
            f' style="--diamond-sheen-delay:{sheen_delay:.3f}s;'
            f'--diamond-sheen-duration:{sheen_duration:.2f}s;'
            f'--diamond-badge-delay:{badge_delay:.3f}s"'
        )

    # Text list only for flavors beyond the 4 shown as images
    flavor_html = ""
    if flavor_extra_count > 0:
        import hashlib
        card_id = hashlib.md5(f"{retailer}{name}{new_price_val}".encode()).hexdigest()[:8]
        flavor_html = f"""
            <div class="flavor-info">
                <button type="button" class="flavor-expand-link" data-card-id="{card_id}" data-extra-count="{flavor_extra_count}" aria-expanded="false" aria-controls="flavors-{card_id}">
                    {flavor_extra_count} more flavors
                </button>
                <div class="flavor-list-expanded" id="flavors-{card_id}" style="display: none;">
                    {''.join([f'<a href="{html.escape(f.get("url", "#"))}" target="_blank" rel="noopener noreferrer" class="flavor-link">{html.escape(f.get("name", ""))}</a>' for f in flavor_extra_data])}
                </div>
            </div>
            """

    return f"""
    <div class="card {tier_class}"{diamond_style_attr}
         data-item-id="{item_id_attr}"
         data-section="{section_attr}"
         data-category="{category_attr}"
         data-retailer="{retailer_attr}"
         data-brand="{brand_attr}"
         data-price="{new_price_val}"
         data-saved="{dollars_saved:.4f}"
         data-percent="{float(percent_off) if percent_off is not None else 0.0}"
         data-deal-count="{deal_count}"
         data-tier="{tier_name}"
         data-search="{search_attr}"
         data-on-sale="1">
        <button type="button" class="sb-star-btn" aria-label="Save to My Items" aria-pressed="false" title="Save to My Items">
          <svg class="sb-star-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
            <path d="M12 3.6l2.47 5.01 5.53.8-4 3.9.94 5.5L12 16.9l-4.94 2.6.94-5.5-4-3.9 5.53-.8L12 3.6z"/>
          </svg>
        </button>
        {badge_html}
        {diamond_sparkles_html}
        <div class="card-image-wrap">
            {image_carousel_html}
            {pack_pill_html}
        </div>
        <div class="card-content">
            <div class="card-pricing">
                {old_price_html}
                <span class="new-price">${new_price_val:.2f}</span>
                {percent_off_html}
            </div>
            {buy_html}
            <div class="card-title">{name}</div>
            {flavor_html}
            <details class="sb-deal-history" hidden><summary>See deal history <span class="sb-hist-chev">▾</span></summary><div class="sb-last-sale-lines"></div></details>
            <div class="card-footer">
                {streak_html}
            </div>
            <a class="{button_class}" href="{retailer_url}" target="_blank" rel="noopener noreferrer">
                View on {retailer_cta_label(deal.get("retailer", ""))}.com
            </a>
        </div>
    </div>
    """


def _best_savings_buy_known(deal: dict) -> bool:
    """
    Strip only shows deals with a concrete how-to-buy path.
    Unknown / check_store_availability stay in the main grid only.
    """
    avail = deal.get("availability") or deal.get("availability_norm") or ""
    if not isinstance(avail, str):
        return False
    val = avail.strip().lower()
    return val in ("both", "in_store", "online_only")


def pick_best_savings_deals(deals: list[dict], limit: int = 6) -> list[dict]:
    """
    Top dollar-savings deals (≥10% off) from today's published set.
    Quarantined / safety-held deals are already absent from deals_today.json.
    One product family (brand + product + pack) only — best flavor/store wins.
    Skips unknown / check_store_availability how-to-buy; backfills next-best.
    Everyday Walmart bundles (flat price 30+ days) are excluded from the strip.
    """
    ranked: list[tuple[float, float, dict]] = []
    for row in deals:
        if row.get("is_everyday_bundle"):
            continue
        if not _best_savings_buy_known(row):
            continue
        try:
            pct = float(row.get("percent_off") or 0.0)
            old_p = float(row.get("old_price"))
            new_p = float(row.get("new_price"))
        except (TypeError, ValueError):
            continue
        if pct < 10.0 or old_p <= new_p:
            continue
        product_name = _norm_str(row.get("product_name"))
        brand = _norm_str(row.get("brand")) or extract_brand_from_product_name(product_name)
        item_id = make_item_id(brand, product_name, row.get("pack_count"))
        saved = old_p - new_p
        enriched = dict(row)
        enriched["_saved"] = saved
        enriched["_item_id"] = item_id
        enriched["_brand"] = brand
        ranked.append((saved, pct, enriched))

    ranked.sort(key=lambda t: (-t[0], -t[1]))
    out: list[dict] = []
    seen_families: set[str] = set()
    for _saved, _pct, row in ranked:
        fam = row["_item_id"]
        if fam in seen_families:
            continue
        seen_families.add(fam)
        out.append(row)
        if len(out) >= limit:
            break
    return out


def build_best_savings_html(
    deals: list[dict], walmart_ctx: dict | None = None
) -> str:
    picks = pick_best_savings_deals(deals, limit=6)
    if not picks:
        return ""

    tiles = []
    for d in picks:
        product_name = remove_pack_size_from_name(_norm_str(d.get("product_name")))
        flavor = _norm_str(d.get("flavor"))
        title = f"{product_name} — {flavor}" if flavor else product_name
        store = display_retailer_label(d.get("retailer", ""))
        img = _norm_str(d.get("image_url") or "")
        url = _norm_str(d.get("retailer_url") or d.get("canonical_url") or "#")
        try:
            new_p = float(d.get("new_price"))
            old_p = float(d.get("old_price"))
            pct = float(d.get("percent_off") or 0.0)
            saved = float(d.get("_saved") or (old_p - new_p))
        except (TypeError, ValueError):
            continue
        item_id = html.escape(d.get("_item_id") or "")
        buy_html = howto_buy_html(d, walmart_ctx=walmart_ctx)
        img_html = (
            f'<img src="{html.escape(img)}" alt="{html.escape(title)}" loading="lazy" decoding="async" />'
            if img
            else '<div class="sb-savings-tile-img-ph" aria-hidden="true"></div>'
        )
        tiles.append(
            f"""
            <article class="sb-savings-tile" data-item-id="{item_id}" data-url="{html.escape(url)}" tabindex="0" role="link" aria-label="{html.escape(title)} at {html.escape(store)}">
              <button type="button" class="sb-star-btn sb-savings-star" aria-label="Save to My Items" aria-pressed="false" title="Save to My Items">
                <svg class="sb-star-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                  <path d="M12 3.6l2.47 5.01 5.53.8-4 3.9.94 5.5L12 16.9l-4.94 2.6.94-5.5-4-3.9 5.53-.8L12 3.6z"/>
                </svg>
              </button>
              <div class="sb-savings-tile-img">{img_html}</div>
              <div class="sb-savings-tile-store">{html.escape(store)}</div>
              <div class="sb-savings-tile-name">{html.escape(title)}</div>
              <div class="sb-savings-tile-save">Save ${saved:.2f}</div>
              <div class="card-pricing">
                <span class="old-price">${old_p:.2f}</span>
                <span class="new-price">${new_p:.2f}</span>
                <span class="percent-off">{pct:.0f}% OFF</span>
              </div>
              {buy_html}
            </article>
            """
        )

    tiles_html = "\n".join(tiles)
    return f"""
        <section class="sb-best-savings" id="sb-best-savings" aria-label="Best savings this week">
            <div class="sb-best-savings-inner">
                <div class="sb-best-savings-header">
                    <div class="sb-best-savings-copy">
                        <h2 class="sb-best-savings-title">Best savings this week</h2>
                        <p class="sb-best-savings-subtitle">The biggest dollar savings at Bowling Green stores right now</p>
                    </div>
                    <div class="sb-best-savings-pill">☆ Star what you buy. We’ll track it for you.</div>
                </div>
                <div class="sb-best-savings-rail" id="sb-best-savings-rail">
                    {tiles_html}
                </div>
                <a class="sb-best-savings-see-all" href="#deals-list">See all deals</a>
            </div>
        </section>
    """


def build_page_html(deals):
    walmart_ctx = load_walmart_buy_context()
    annotate_everyday_bundles(deals, walmart_ctx=walmart_ctx)
    deals = filter_walmart_bundles_from_deals(deals)
    # Group deals into deal families
    grouped_deals = group_deals(deals)
    # Propagate everyday-bundle flag onto grouped cards (any flavor counts).
    for g in grouped_deals:
        flavors = g.get("flavor_data") or []
        g["is_everyday_bundle"] = bool(g.get("is_everyday_bundle")) or any(
            bool(f.get("is_everyday_bundle")) for f in flavors
        )
        if g.get("bundle_n") is None:
            for f in flavors:
                if f.get("bundle_n"):
                    g["bundle_n"] = f.get("bundle_n")
                    break
    # Calculate total number of individual deals (flavors) across all groups
    total_deals = sum(
        len(g.get("flavor_data", [])) if g.get("flavor_data") else 1
        for g in grouped_deals
    )
    last_updated = get_last_updated_text(deals)
    last_updated_short = get_last_updated_short(deals)

    # Sort: tier (diamond first, then fire, then strong, then sale) → best savings → price
    # Everyday bundles sort after sale tiers.
    def sort_key(d):
        if d.get("is_everyday_bundle"):
            tier_priority = 0
            percent = 0.0
        else:
            percent = float(d.get("percent_off") or 0.0)
            # Tier priority: diamond (4) > fire (3) > strong (2) > sale (1)
            if percent >= 25.0:
                tier_priority = 4
            elif percent >= 20.0:
                tier_priority = 3
            elif percent >= 10.0:
                tier_priority = 2
            else:
                tier_priority = 1
        return (
            -tier_priority,  # Higher tier first
            -percent,  # Then by percent off (best deals first)
            float(d.get("new_price") or 0.0),
        )

    deals_sorted = sorted(grouped_deals, key=sort_key)
    cards_html = "\n".join(
        build_card_html(d, walmart_ctx=walmart_ctx) for d in deals_sorted
    )
    best_savings_html = build_best_savings_html(deals, walmart_ctx=walmart_ctx)
    tracked_count, store_count = active_catalog_stats()
    # Individual on-sale items (flavor/pack/store), same units as tracked_count —
    # not grouped card count.
    deal_count = len(deals)
    proof_line = html.escape(
        proof_of_work_line(
            tracked_count=tracked_count,
            store_count=store_count,
            deal_count=deal_count,
        )
    )
    catalog_count = 0
    catalog_json_path = DOCS_DIR / "catalog.json"
    if catalog_json_path.is_file():
        try:
            catalog_count = int(
                (json.loads(catalog_json_path.read_text(encoding="utf-8")) or {}).get("count")
                or 0
            )
        except Exception:
            catalog_count = 0
    catalog_count_fmt = f"{catalog_count:,}" if catalog_count else "0"
    print(
        f"PROOF_OF_WORK: tracked={tracked_count} stores={store_count} "
        f"on_sale={deal_count} cards={len(deals_sorted)} catalog={catalog_count}",
        flush=True,
    )
    supabase_url = html.escape(SUPABASE_URL)
    supabase_anon_key = html.escape(SUPABASE_ANON_KEY)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8" />
    <title>SnackBuddy Daily Deals</title>
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <link rel="preconnect" href="https://fonts.googleapis.com" />
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
    <link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600;9..144,700&family=Outfit:wght@400;500;600;700&display=swap" rel="stylesheet" />
    <!-- Google tag (gtag.js) -->
    <script async src="https://www.googletagmanager.com/gtag/js?id=G-N900K39W95"></script>
    <script>
      window.dataLayer = window.dataLayer || [];
      function gtag(){{dataLayer.push(arguments);}}
      gtag('js', new Date());
      gtag('config', 'G-N900K39W95');
    </script>
    <style>
        :root {{
            --bg: #f3f4f6;
            --card-bg: #ffffff;
            --border-subtle: #e5e7eb;
            --text-main: #111827;
            --text-muted: #6b7280;
            --blue: #2563eb;
            --green: #16a34a;
            --red: #dc2626;
            --orange: #f97316;
            --yellow: #facc15;
            --shadow-soft: 0 8px 20px rgba(15, 23, 42, 0.08);
            --radius-lg: 16px;
            --radius-pill: 999px;
            --navy: #0f172a;
            --page-max: 1320px;
            --sb-header-h: 72px;
        }}

        .visually-hidden {{
            position: absolute !important;
            width: 1px !important;
            height: 1px !important;
            padding: 0 !important;
            margin: -1px !important;
            overflow: hidden !important;
            clip: rect(0, 0, 0, 0) !important;
            white-space: nowrap !important;
            border: 0 !important;
        }}

        * {{
            box-sizing: border-box;
        }}

        body {{
            margin: 0;
            font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI",
                sans-serif;
            background-color: var(--bg);
            color: var(--text-main);
        }}

        .page {{
            max-width: var(--page-max);
            margin: 0 auto 40px auto;
            padding: 0 16px 40px 16px;
        }}

        /* ============================ */
        /* SECTION 1A: SITE HEADER NAV  */
        /* ============================ */

        .sb-header {{
            position: sticky;
            top: 0;
            z-index: 40;
            background-color: #ffffff;
            border-bottom: 1px solid var(--border-subtle);
        }}

        .sb-header-inner {{
            max-width: var(--page-max);
            margin: 0 auto;
            padding: 10px 16px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
        }}

        .sb-header-left {{
            display: flex;
            align-items: center;
            gap: 8px;
            min-width: 0;
        }}

        /* Logo image (replaces placeholder box) */
        .sb-logo {{
            width: 48px;
            height: 48px;
            border-radius: 12px;
            object-fit: contain;
            display: block;
            flex-shrink: 0;
        }}

        .sb-header-title {{
            font-weight: 700;
            font-size: 22px;
            color: var(--text-main);
            letter-spacing: -0.02em;
        }}

        .sb-header-freshness {{
            display: flex;
            flex-direction: column;
            align-items: flex-end;
            gap: 1px;
            margin-left: auto;
            margin-right: 8px;
            min-width: 0;
            text-align: right;
        }}

        .sb-header-freshness-label {{
            font-size: 10px;
            font-weight: 600;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            color: #15803d;
            line-height: 1.2;
        }}

        .sb-header-freshness-time {{
            font-size: 12px;
            font-weight: 600;
            color: var(--text-main);
            line-height: 1.2;
            white-space: nowrap;
        }}

        @media (max-width: 520px) {{
            .sb-header-freshness-time {{
                font-size: 11px;
                white-space: normal;
            }}
        }}

        .sb-menu-button {{
            border: none;
            background: transparent;
            font-size: 20px;
            cursor: pointer;
            flex-shrink: 0;
        }}

        /* Slide-out nav drawer */

        .sb-nav-backdrop {{
            position: fixed;
            inset: 0;
            background: rgba(15, 23, 42, 0.35);
            opacity: 0;
            pointer-events: none;
            transition: opacity 0.18s ease-out;
            z-index: 39;
        }}

        .sb-nav-backdrop.open {{
            opacity: 1;
            pointer-events: auto;
        }}

        .sb-nav-drawer {{
            position: fixed;
            top: 0;
            right: 0;
            height: 100vh;
            width: 260px;
            background: #ffffff;
            box-shadow: -8px 0 20px rgba(15,23,42,0.25);
            transform: translateX(100%);
            transition: transform 0.2s ease-out;
            z-index: 40;
            display: flex;
            flex-direction: column;
        }}

        .sb-nav-drawer.open {{
            transform: translateX(0%);
        }}

        .sb-nav-inner {{
            padding: 16px 18px;
            display: flex;
            flex-direction: column;
            gap: 12px;
            height: 100%;
        }}

        .sb-nav-header {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 8px;
        }}

        .sb-nav-header-left {{
            display: flex;
            align-items: center;
            gap: 8px;
        }}

        .sb-nav-title {{
            font-weight: 600;
            font-size: 18px;
        }}

        .sb-nav-close {{
            border: none;
            background: transparent;
            font-size: 20px;
            cursor: pointer;
        }}

        .sb-nav-link {{
            display: block;
            padding: 8px 0;
            font-size: 14px;
            color: var(--text-main);
            text-decoration: none;
        }}

        .sb-nav-link:hover {{
            color: var(--blue);
        }}

        .sb-nav-footer {{
            margin-top: auto;
            font-size: 12px;
            color: var(--text-muted);
            line-height: 1.35;
        }}

/* ============================ */
/* FILTER BAR (Walmart-style pills) */
/* ============================ */

.sb-filter-bar-section {{
  max-width: var(--page-max);
  margin: 4px auto 10px;
  padding: 0 8px 0 4px;
  position: relative;
  z-index: 30;
}}

.sb-filter-toggle-wrap {{
  flex: 0 0 auto;
}}

.sb-filter-master {{
  position: relative;
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  width: 40px;
  height: 40px;
  padding: 0;
  border: 1px solid #111827;
  border-radius: 999px;
  background: #ffffff;
  cursor: pointer;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.06);
  color: #111827;
  font-family: inherit;
}}

.sb-filter-master-label {{
  display: none;
  font-size: 13px;
  font-weight: 600;
  line-height: 1;
}}

.sb-results-count {{
  display: none;
  margin-left: auto;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-muted);
  white-space: nowrap;
}}

.sb-filter-sidebar {{
  display: none;
}}

/* ============================ */
/* CHIP ROW + FILTER SHEET          */
/* ============================ */

.sb-chip-row-section {{
  max-width: var(--page-max);
  margin: 4px auto 6px;
  padding: 0 8px 0 4px;
  position: relative;
  z-index: 30;
}}

.sb-chip-row-bar {{
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}}

.sb-chip-row-scroll {{
  position: relative;
  flex: 1 1 auto;
  min-width: 0;
}}

.sb-chip-row-scroll.has-scroll-prev::before,
.sb-chip-row-scroll.has-scroll-next::after {{
  content: "";
  position: absolute;
  top: 0;
  bottom: 0;
  width: 22px;
  z-index: 2;
  pointer-events: none;
}}

.sb-chip-row-scroll.has-scroll-prev::before {{
  left: 0;
  background: linear-gradient(to right, var(--bg) 15%, rgba(243, 244, 246, 0));
}}

.sb-chip-row-scroll.has-scroll-next::after {{
  right: 0;
  background: linear-gradient(to left, var(--bg) 15%, rgba(243, 244, 246, 0));
}}

.sb-chip-row-track {{
  overflow-x: auto;
  overflow-y: hidden;
  scroll-behavior: smooth;
  -webkit-overflow-scrolling: touch;
  scrollbar-width: none;
}}

.sb-chip-row-track::-webkit-scrollbar {{
  display: none;
}}

.sb-chip-row-inner {{
  display: flex;
  flex-wrap: nowrap;
  align-items: center;
  gap: 6px;
  padding: 7px 4px 5px;
  min-width: min-content;
}}

.sb-chip-row-actions {{
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 6px;
  padding: 7px 0 5px;
  background: var(--bg);
  z-index: 3;
}}

.sb-category-chips {{
  display: contents;
}}

.sb-chip-divider {{
  flex: 0 0 1px;
  align-self: stretch;
  margin: 5px 2px;
  background: #d1d5db;
}}

.sb-chip {{
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex: 0 0 auto;
  border: 1px solid #111827;
  background: #ffffff;
  color: #111827;
  border-radius: 999px;
  padding: 9px 14px;
  font-size: 14px;
  font-weight: 600;
  font-family: inherit;
  line-height: 1.2;
  white-space: nowrap;
  cursor: pointer;
  transition: background 0.12s ease, border-color 0.12s ease, color 0.12s ease, box-shadow 0.12s ease;
}}

.sb-chip[hidden] {{
  display: none;
}}

.sb-chip:hover {{
  background: #f9fafb;
}}

.sb-chip.is-active,
.sb-chip[aria-expanded="true"] {{
  background: #A2B88E;
  border-color: #6f8a5a;
  color: #14210c;
  box-shadow: inset 0 0 0 1px #6f8a5a;
}}

.sb-chip-caret {{
  font-size: 11px;
  line-height: 1;
}}

.sb-chip-icon {{
  min-width: 40px;
  justify-content: center;
  padding: 8px 11px;
  font-size: 16px;
}}

.sb-chip-exit {{
  border-color: #f59e0b;
  background: #fffbeb;
  color: #b45309;
}}

.sb-chip-badge {{
  position: absolute;
  top: -4px;
  right: -4px;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  border-radius: 999px;
  background: #111827;
  color: #ffffff;
  font-size: 11px;
  font-weight: 800;
  line-height: 18px;
  text-align: center;
}}

.sb-chip-badge[hidden] {{
  display: none;
}}

.sb-chip-meta {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 2px 6px 0 4px;
}}

.sb-legend-link {{
  border: none;
  background: transparent;
  padding: 2px 0;
  color: #4b6638;
  font-size: 13px;
  font-weight: 600;
  font-family: inherit;
  text-decoration: underline;
  cursor: pointer;
}}

.sb-chip-meta .sb-results-count {{
  display: inline;
  margin-left: auto;
}}

.sb-badge-legend {{
  margin: 6px 4px 4px;
  padding: 10px 14px;
  background: #ffffff;
  border: 1px solid #A2B88E;
  border-radius: 12px;
  font-size: 13px;
  color: #334155;
}}

.sb-badge-legend[hidden] {{
  display: none;
}}

.sb-badge-legend-list {{
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 4px;
  line-height: 1.35;
}}

.sb-chip-row-section .sb-search-wrap {{
  margin: 4px 4px 2px;
  position: relative;
}}

.sb-chip-row-section .sb-search-wrap[hidden] {{
  display: none;
}}

.sb-chip-row-section .sb-search-input:focus {{
  border-color: #A2B88E;
  box-shadow: 0 0 0 3px rgba(162, 184, 142, 0.35);
}}

.sb-chip-row-section .sb-search-clear {{
  display: block;
}}

.sb-stores-popover {{
  min-width: 240px;
  padding: 12px;
}}

.sb-popover-title {{
  font-size: 13px;
  font-weight: 800;
  margin-bottom: 6px;
  color: var(--text-main);
}}

.sb-popover-footer {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-top: 8px;
  padding-top: 10px;
  border-top: 1px solid var(--border-subtle);
}}

.sb-popover-link {{
  border: none;
  background: transparent;
  color: #4b6638;
  font-size: 12px;
  font-weight: 700;
  font-family: inherit;
  text-decoration: underline;
  cursor: pointer;
  padding: 0;
}}

.sb-popover-done {{
  border: 1px solid #6f8a5a;
  background: #A2B88E;
  color: #14210c;
  border-radius: 999px;
  padding: 8px 18px;
  font-size: 13px;
  font-weight: 800;
  font-family: inherit;
  cursor: pointer;
}}

.sb-filter-check input {{
  accent-color: #6f8a5a;
}}

.sb-filter-sheet-scroll {{
  max-height: 220px;
  overflow-y: auto;
}}

.sb-deals-shell {{
  display: block;
}}

.sb-filter-sidebar-head {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--border-subtle);
}}

.sb-filter-sidebar-title {{
  font-size: 14px;
  font-weight: 700;
  color: var(--text-main);
}}

.sb-filter-sidebar-clear {{
  border: none;
  background: transparent;
  color: #2563eb;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  padding: 0;
  font-family: inherit;
}}

.sb-filter-sidebar-clear:hover {{
  text-decoration: underline;
}}

.sb-filter-acc {{
  border-bottom: 1px solid #eef2f7;
  padding: 4px 0 8px;
}}

.sb-filter-acc:last-child {{
  border-bottom: none;
}}

.sb-filter-acc > summary {{
  list-style: none;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 10px 2px;
  font-size: 13px;
  font-weight: 700;
  color: var(--text-main);
  user-select: none;
}}

.sb-filter-acc > summary::-webkit-details-marker {{
  display: none;
}}

.sb-filter-acc > summary::after {{
  content: "▾";
  font-size: 12px;
  color: #64748b;
  transition: transform 0.15s ease;
}}

.sb-filter-acc[open] > summary::after {{
  transform: rotate(180deg);
}}

.sb-filter-acc-body {{
  padding: 0 2px 6px;
  max-height: 220px;
  overflow: auto;
}}

.sb-filter-acc-body .sb-filter-check {{
  justify-content: space-between;
  padding: 5px 0;
  font-size: 13px;
  color: #334155;
}}

.sb-filter-acc-count {{
  color: #94a3b8;
  font-size: 12px;
  font-weight: 600;
  margin-left: 8px;
}}

.sb-filter-bar {{
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}}

.sb-filter-master:hover {{
  filter: brightness(0.98);
}}

.sb-filter-master-icon {{
  width: 20px;
  height: 20px;
  display: block;
  color: #111827;
}}

.sb-filter-master-badge,
.sb-filter-pill-badge {{
  position: absolute;
  top: -4px;
  right: -4px;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  border-radius: 999px;
  background: #111827;
  color: #ffffff;
  font-size: 11px;
  font-weight: 800;
  line-height: 18px;
  text-align: center;
}}

.sb-filter-pill-badge {{
  top: -6px;
  right: -6px;
}}

.sb-filter-scroll-btn {{
  position: absolute;
  top: 50%;
  transform: translateY(-50%);
  z-index: 4;
  flex-shrink: 0;
  width: 28px;
  height: 28px;
  padding: 0;
  border: 1px solid #cbd5e1;
  border-radius: 999px;
  background: #ffffff;
  color: #0f172a;
  font-size: 16px;
  font-weight: 700;
  line-height: 1;
  cursor: pointer;
  display: none;
  align-items: center;
  justify-content: center;
  box-shadow: 0 1px 3px rgba(15, 23, 42, 0.14);
  opacity: 1;
}}

.sb-filter-scroll-btn:hover,
.sb-filter-scroll-btn:focus-visible {{
  border-color: #94a3b8;
  background: #f8fafc;
}}

.sb-filter-scroll-btn.is-visible {{
  display: inline-flex;
}}

.sb-filter-scroll-prev {{
  left: 2px;
}}

.sb-filter-scroll-next {{
  right: 2px;
}}

.sb-filter-bar-scroll {{
  position: relative;
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
}}

/* Soft fade instead of hard cut-off when more pills sit off-screen */
.sb-filter-bar-scroll.has-scroll-prev::before,
.sb-filter-bar-scroll.has-scroll-next::after {{
  content: "";
  position: absolute;
  top: 0;
  bottom: 0;
  width: 20px;
  z-index: 2;
  pointer-events: none;
}}

.sb-filter-bar-scroll.has-scroll-prev::before {{
  left: 0;
  background: linear-gradient(to right, var(--bg) 15%, rgba(243, 244, 246, 0));
}}

.sb-filter-bar-scroll.has-scroll-next::after {{
  right: 0;
  background: linear-gradient(to left, var(--bg) 15%, rgba(243, 244, 246, 0));
}}

.sb-filter-bar-scroll.has-scroll-prev .sb-filter-bar-track {{
  mask-image: linear-gradient(to right, transparent 0, #000 16px);
  -webkit-mask-image: linear-gradient(to right, transparent 0, #000 16px);
  padding-left: 26px;
}}

.sb-filter-bar-scroll.has-scroll-next .sb-filter-bar-track {{
  mask-image: linear-gradient(to left, transparent 0, #000 16px);
  -webkit-mask-image: linear-gradient(to left, transparent 0, #000 16px);
  padding-right: 26px;
}}

.sb-filter-bar-scroll.has-scroll-prev.has-scroll-next .sb-filter-bar-track {{
  mask-image: linear-gradient(to right, transparent 0, #000 16px, #000 calc(100% - 16px), transparent);
  -webkit-mask-image: linear-gradient(to right, transparent 0, #000 16px, #000 calc(100% - 16px), transparent);
}}

.sb-filter-bar-track {{
  flex: 1;
  min-width: 0;
  overflow-x: auto;
  overflow-y: visible;
  scroll-behavior: smooth;
  -webkit-overflow-scrolling: touch;
  scrollbar-width: none;
}}

.sb-filter-bar-track::-webkit-scrollbar {{
  display: none;
}}

.sb-filter-bar-inner {{
  display: flex;
  flex-wrap: nowrap;
  align-items: center;
  gap: 6px;
  padding: 4px 0;
  min-width: min-content;
}}

@media (max-width: 640px) {{
  .sb-filter-bar-section {{
    padding: 0 6px 0 2px;
  }}

  .sb-filter-bar {{
    gap: 4px;
  }}

  .sb-filter-master {{
    width: 38px;
    height: 38px;
  }}

  .sb-filter-pill {{
    padding: 9px 12px;
    font-size: 13px;
  }}
}}

.sb-filter-pill-wrap {{
  position: relative;
  flex-shrink: 0;
}}

.sb-filter-pill {{
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 10px 14px;
  border: 1px solid #111827;
  border-radius: 999px;
  background: #ffffff;
  color: #111827;
  font-size: 14px;
  font-weight: 600;
  white-space: nowrap;
  cursor: pointer;
  font-family: inherit;
  line-height: 1.2;
}}

.sb-filter-pill:hover {{
  background: #f9fafb;
}}

.sb-filter-pill[aria-expanded="true"] {{
  background: #f3f4f6;
  box-shadow: inset 0 0 0 1px #111827;
}}

.sb-filter-pill-chevron {{
  font-size: 12px;
  color: #374151;
  line-height: 1;
}}

.sb-filter-dropdown-panel {{
  position: fixed;
  z-index: 46;
  min-width: 220px;
  max-width: min(320px, calc(100vw - 16px));
  max-height: min(320px, calc(100vh - 16px));
  overflow-y: auto;
  padding: 10px;
  border: 1px solid var(--border-subtle);
  border-radius: 12px;
  background: #ffffff;
  box-shadow: 0 10px 28px rgba(15, 23, 42, 0.14);
}}

.sb-filter-dropdown-panel[hidden] {{
  display: none !important;
}}

.sb-filter-dropdown-empty {{
  padding: 8px 4px;
  font-size: 13px;
  color: var(--text-muted);
  line-height: 1.4;
}}

.sb-filter-check {{
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 4px;
  font-size: 14px;
  cursor: pointer;
}}

.sb-filter-check input {{
  width: 16px;
  height: 16px;
  flex-shrink: 0;
}}

.sb-filter-dropdown-backdrop {{
  position: fixed;
  inset: 0;
  z-index: 44;
  background: transparent;
  display: none;
}}

.sb-filter-dropdown-backdrop.open {{
  display: block;
}}

.sb-filter-sheet-backdrop {{
  position: fixed;
  inset: 0;
  z-index: 48;
  background: rgba(15, 23, 42, 0.35);
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.18s ease-out;
}}

.sb-filter-sheet-backdrop.open {{
  opacity: 1;
  pointer-events: auto;
}}

.sb-filter-sheet {{
  position: fixed;
  left: 0;
  right: 0;
  bottom: 0;
  z-index: 49;
  max-height: 85vh;
  background: #ffffff;
  border-radius: 18px 18px 0 0;
  box-shadow: 0 -8px 28px rgba(15, 23, 42, 0.18);
  transform: translateY(105%);
  transition: transform 0.22s ease-out;
  display: flex;
  flex-direction: column;
}}

.sb-filter-sheet.open {{
  transform: translateY(0);
}}

.sb-filter-sheet-header {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 16px;
  border-bottom: 1px solid var(--border-subtle);
}}

.sb-filter-sheet-title {{
  font-size: 18px;
  font-weight: 800;
}}

.sb-filter-sheet-close {{
  border: none;
  background: transparent;
  font-size: 24px;
  cursor: pointer;
  line-height: 1;
}}

.sb-filter-sheet-body {{
  overflow-y: auto;
  padding: 12px 16px 16px;
}}

.sb-filter-sheet-group {{
  margin-bottom: 14px;
}}

.sb-filter-sheet-group-title {{
  font-size: 13px;
  font-weight: 800;
  margin-bottom: 8px;
}}

.sb-filter-sheet-footer {{
  display: flex;
  gap: 10px;
  padding: 12px 16px 16px;
  border-top: 1px solid var(--border-subtle);
}}

.sb-filter-sheet-clear,
.sb-filter-sheet-done {{
  flex: 1;
  border-radius: 12px;
  padding: 12px;
  font-weight: 800;
  font-size: 14px;
  cursor: pointer;
  border: 1px solid var(--border-subtle);
  background: #ffffff;
}}

.sb-filter-sheet-done {{
  background: var(--navy);
  color: #ffffff;
  border-color: var(--navy);
}}

@media (min-width: 992px) {{
  .sb-filter-sheet {{
    left: 50%;
    right: auto;
    bottom: auto;
    top: 50%;
    width: min(420px, 92vw);
    max-height: 80vh;
    border-radius: 16px;
    transform: translate(-50%, -50%) scale(0.96);
    opacity: 0;
    pointer-events: none;
  }}
  .sb-filter-sheet.open {{
    transform: translate(-50%, -50%) scale(1);
    opacity: 1;
    pointer-events: auto;
  }}
  .sb-hero {{
    position: relative;
    z-index: 1;
  }}
}}

.sb-deals-shell {{
  max-width: var(--page-max);
  margin: 0 auto;
  padding: 0 16px;
}}

.sb-deals-main {{
  min-width: 0;
}}

.sb-deals-main .card-grid {{
  max-width: none;
  margin: 0;
}}

        /* ============================ */
        /* SECTION 1B: HERO BANNER      */
        /* ============================ */

        .sb-hero {{
            margin-top: 8px;
        }}

        .sb-hero-bg {{
            max-width: var(--page-max);
            margin: 0 auto;
            border-radius: 18px;
            padding: 18px 18px 14px;
            position: relative;
            overflow: hidden;
            background: linear-gradient(145deg, #e8f6ee 0%, #fafcfa 48%, #eef7f1 100%);
            color: #111827;
            box-shadow: 0 6px 16px rgba(15, 23, 42, 0.08);
            border: 1px solid #d8ebe0;
        }}

        /* wallpaper brand texture */
        .sb-hero-pattern {{
            position: absolute;
            inset: 0;
            opacity: 0.30;
            background-image: url("assets/hero-wallpaper.png");
            background-repeat: repeat;
            background-size: 360px auto;
            background-position: top left;
            /* Push muted grey doodles toward brand green */
            filter: brightness(1.08) contrast(1.05) sepia(0.48) saturate(2.4) hue-rotate(68deg);
            pointer-events: none;
        }}

        /* soft wash so text stays readable without killing the green */
        .sb-hero-bg::after {{
            content: "";
            position: absolute;
            inset: 0;
            background: linear-gradient(
                180deg,
                rgba(255, 255, 255, 0.38) 0%,
                rgba(247, 252, 249, 0.22) 100%
            );
            pointer-events: none;
        }}

        .sb-hero-content {{
            position: relative;
            z-index: 2;
        }}

        .sb-hero-kicker {{
            font-size: 12px;
            color: #374151;
            margin-bottom: 4px;
        }}

        .sb-hero-freshness {{
            display: none; /* header already shows prices-checked */
        }}

        .sb-hero-freshness-dot {{
            width: 7px;
            height: 7px;
            border-radius: 50%;
            background: #22c55e;
            flex-shrink: 0;
            box-shadow: 0 0 0 3px rgba(34, 197, 94, 0.22);
        }}

        .sb-best-savings {{
            max-width: var(--page-max);
            margin: 14px auto 0;
            padding: 0 16px;
        }}
        .sb-best-savings-header {{
            display: flex;
            flex-wrap: wrap;
            align-items: flex-start;
            justify-content: space-between;
            gap: 10px 16px;
            margin-bottom: 12px;
        }}
        .sb-best-savings-title {{
            margin: 0;
            font-size: 20px;
            font-weight: 700;
            color: var(--text-main);
            letter-spacing: -0.02em;
        }}
        .sb-best-savings-subtitle {{
            margin: 4px 0 0;
            font-size: 14px;
            color: var(--text-muted);
            line-height: 1.35;
        }}
        .sb-best-savings-pill {{
            display: inline-flex;
            align-items: center;
            padding: 6px 12px;
            border-radius: var(--radius-pill);
            border: 1px solid var(--border-subtle);
            background: #ffffff;
            box-shadow: 0 1px 2px rgba(15, 23, 42, 0.06);
            font-size: 12px;
            font-weight: 600;
            color: var(--text-muted);
            white-space: nowrap;
        }}
        .sb-best-savings-rail {{
            display: grid;
            grid-template-columns: repeat(6, minmax(0, 1fr));
            gap: 12px;
        }}
        .sb-savings-tile {{
            position: relative;
            background: var(--card-bg);
            border: 1px solid var(--border-subtle);
            border-radius: var(--radius-lg);
            box-shadow: var(--shadow-soft);
            padding: 10px 10px 12px;
            display: flex;
            flex-direction: column;
            gap: 4px;
            cursor: pointer;
            color: inherit;
            min-width: 0;
            transition: transform 0.12s ease, box-shadow 0.12s ease;
        }}
        .sb-savings-tile:hover {{
            transform: translateY(-1px);
            box-shadow: 0 10px 24px rgba(15, 23, 42, 0.12);
        }}
        .sb-savings-tile:focus-visible {{
            outline: 2px solid var(--green);
            outline-offset: 2px;
        }}
        .sb-savings-star {{
            width: 44px;
            height: 44px;
            top: 8px;
            right: 8px;
        }}
        .sb-savings-tile-img {{
            width: 100%;
            aspect-ratio: 1 / 1;
            border-radius: 12px;
            overflow: hidden;
            background: #f8fafc;
            display: flex;
            align-items: center;
            justify-content: center;
        }}
        .sb-savings-tile-img img {{
            width: 100%;
            height: 100%;
            object-fit: contain;
            display: block;
        }}
        .sb-savings-tile-img-ph {{
            width: 40%;
            height: 40%;
            border-radius: 8px;
            background: #e5e7eb;
        }}
        .sb-savings-tile-store {{
            margin-top: 4px;
            font-size: 11px;
            font-weight: 600;
            color: var(--text-muted);
            letter-spacing: 0.01em;
        }}
        .sb-savings-tile-name {{
            font-size: 13px;
            font-weight: 600;
            line-height: 1.25;
            color: var(--text-main);
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
            overflow: hidden;
            min-height: 2.5em;
        }}
        .sb-savings-tile-save {{
            margin-top: 2px;
            font-size: 22px;
            font-weight: 800;
            color: var(--green);
            letter-spacing: -0.02em;
            line-height: 1.1;
        }}
        .sb-savings-tile .card-pricing {{
            flex-wrap: nowrap;
            white-space: nowrap;
        }}
        .sb-best-savings-see-all {{
            display: inline-block;
            margin-top: 12px;
            font-size: 14px;
            font-weight: 600;
            color: var(--blue);
            text-decoration: none;
        }}
        .sb-best-savings-see-all:hover {{
            text-decoration: underline;
        }}
        @media (max-width: 900px) {{
            .sb-best-savings-rail {{
                display: flex;
                flex-wrap: nowrap;
                gap: 12px;
                overflow-x: auto;
                -webkit-overflow-scrolling: touch;
                scroll-snap-type: x mandatory;
                padding-bottom: 6px;
            }}
            .sb-savings-tile {{
                flex: 0 0 200px;
                width: 200px;
                scroll-snap-align: start;
            }}
            .sb-savings-tile .card-pricing {{
                font-size: 11px;
                gap: 4px;
            }}
            .sb-savings-tile .card-pricing .percent-off {{
                font-size: 14px;
            }}
            .sb-best-savings-title {{
                font-size: 18px;
            }}
            .sb-best-savings-pill {{
                white-space: normal;
            }}
        }}

        .sb-tools-bar {{
            max-width: var(--page-max);
            margin: 0 auto 8px;
            padding: 0 8px 0 4px;
            display: flex;
            flex-wrap: nowrap;
            gap: 8px;
            align-items: center;
        }}

        .sb-search-wrap {{
            flex: 1 1 auto;
            min-width: 0;
            position: relative;
        }}

        .sb-search-input {{
            width: 100%;
            box-sizing: border-box;
            border: 1px solid var(--border-subtle);
            background: #fff;
            border-radius: 999px;
            padding: 9px 36px 9px 14px;
            font-size: 14px;
            color: var(--text-main);
            outline: none;
        }}

        .sb-search-input:focus {{
            border-color: #86efac;
            box-shadow: 0 0 0 3px rgba(34, 197, 94, 0.15);
        }}

        .sb-search-clear {{
            position: absolute;
            right: 8px;
            top: 50%;
            transform: translateY(-50%);
            border: none;
            background: transparent;
            color: var(--text-muted);
            font-size: 18px;
            line-height: 1;
            cursor: pointer;
            padding: 4px 6px;
            display: none;
        }}

        .sb-search-wrap.has-value .sb-search-clear {{
            display: block;
        }}

        .sb-sort-wrap {{
            flex: 0 0 auto;
            position: relative;
        }}

        .sb-sort-pill {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            border: 1px solid #111827;
            background: #ffffff;
            color: #111827;
            border-radius: 999px;
            padding: 10px 14px;
            font-size: 14px;
            font-weight: 600;
            font-family: inherit;
            line-height: 1.2;
            cursor: pointer;
            white-space: nowrap;
        }}

        .sb-sort-pill:hover {{
            background: #f9fafb;
        }}

        .sb-sort-pill[aria-expanded="true"] {{
            background: #f3f4f6;
            box-shadow: inset 0 0 0 1px #111827;
        }}

        .sb-sort-menu {{
            position: absolute;
            right: 0;
            top: calc(100% + 6px);
            z-index: 45;
            min-width: 220px;
            padding: 6px;
            border: 1px solid var(--border-subtle);
            border-radius: 12px;
            background: #ffffff;
            box-shadow: 0 10px 28px rgba(15, 23, 42, 0.14);
        }}

        .sb-sort-menu[hidden] {{
            display: none;
        }}

        .sb-sort-option {{
            display: block;
            width: 100%;
            text-align: left;
            border: none;
            background: transparent;
            border-radius: 8px;
            padding: 10px 12px;
            font-size: 13px;
            font-weight: 600;
            font-family: inherit;
            color: #111827;
            cursor: pointer;
        }}

        .sb-sort-option:hover {{
            background: #f3f4f6;
        }}

        .sb-sort-option.is-selected {{
            background: #111827;
            color: #ffffff;
        }}

        .sb-sort-select {{
            /* Kept for JS; UI is the Sort pill + menu */
            position: absolute;
            width: 1px;
            height: 1px;
            opacity: 0;
            pointer-events: none;
        }}

        @media (max-width: 640px) {{
            .sb-tools-bar {{
                padding: 0 6px 0 2px;
                gap: 6px;
            }}

            .sb-sort-pill {{
                padding: 9px 12px;
                font-size: 13px;
            }}

            .sb-hero-bg {{
                padding: 14px 14px 12px;
            }}
        }}

        .sb-deals-empty {{
            display: none;
            max-width: var(--page-max);
            margin: 8px auto 18px;
            padding: 18px 16px;
            text-align: center;
            color: var(--text-muted);
            font-size: 14px;
            border: 1px dashed var(--border-subtle);
            border-radius: 12px;
            background: #fafafa;
        }}

        .sb-deals-empty.is-visible {{
            display: block;
        }}
        .sb-browse-catalog {{
            max-width: var(--page-max);
            margin: 8px auto 18px;
            padding: 0 12px;
            text-align: center;
        }}
        .sb-browse-catalog-link {{
            appearance: none;
            border: none;
            background: none;
            padding: 0;
            font: inherit;
            font-size: 14px;
            font-weight: 600;
            color: var(--blue);
            text-decoration: underline;
            text-underline-offset: 2px;
            cursor: pointer;
        }}
        .sb-browse-catalog-link:hover {{
            color: var(--navy);
        }}
        body.sb-catalog-open .sb-hero,
        body.sb-catalog-open #sb-best-savings,
        body.sb-catalog-open #sb-chip-row,
        body.sb-catalog-open #sb-deals-shell,
        body.sb-catalog-open #sb-my-items-notify,
        body.sb-catalog-open #sb-auth-line,
        body.sb-catalog-open .sb-browse-catalog {{
            display: none !important;
        }}
        .sb-catalog {{
            max-width: var(--page-max);
            margin: 0 auto 28px;
            padding: 10px 12px 96px;
            scroll-margin-top: var(--sb-header-h);
        }}
        .sb-catalog[hidden] {{
            display: none !important;
        }}
        .sb-catalog-topbar {{
            position: sticky;
            top: var(--sb-header-h);
            z-index: 35;
            background: var(--bg);
            /* Bleed into horizontal padding so rows scroll under a solid bar */
            margin: 0 -12px 8px;
            padding: 10px 12px 12px;
            border-bottom: 1px solid rgba(15, 23, 42, 0.06);
        }}
        .sb-catalog-back {{
            appearance: none;
            border: none;
            background: none;
            padding: 0;
            margin: 0 0 10px;
            font: inherit;
            font-size: 14px;
            font-weight: 600;
            color: var(--blue);
            cursor: pointer;
            display: block;
            min-height: 44px;
            line-height: 44px;
        }}
        .sb-catalog-title {{
            font-family: "Fraunces", Georgia, serif;
            font-size: 1.6rem;
            margin: 0 0 12px;
            color: var(--navy);
        }}
        .sb-catalog-search-wrap {{
            margin: 0;
        }}
        .sb-catalog-search {{
            width: 100%;
            box-sizing: border-box;
            padding: 12px 14px;
            border-radius: 14px;
            border: 1.5px solid #cbd5e1;
            font: inherit;
            font-size: 16px;
            color: var(--navy);
            background: #fff;
        }}
        .sb-catalog-search:focus {{
            outline: 2px solid rgba(37, 99, 235, 0.28);
            border-color: var(--blue);
        }}
        .sb-catalog-chips {{
            display: flex;
            flex-wrap: nowrap;
            gap: 8px;
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
            scrollbar-width: none;
            padding: 2px 0 10px;
        }}
        .sb-catalog-chips::-webkit-scrollbar {{ display: none; }}
        .sb-catalog-stores-row {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 10px;
            margin: 0 0 12px;
        }}
        .sb-catalog-count {{
            font-size: 13px;
            font-weight: 600;
            color: var(--text-muted);
            white-space: nowrap;
        }}
        .sb-catalog-list {{
            display: grid;
            grid-template-columns: 1fr;
            gap: 8px;
            align-items: stretch;
            padding-bottom: 88px;
        }}
        @media (min-width: 640px) {{
            .sb-catalog-list {{
                grid-template-columns: repeat(2, minmax(0, 1fr));
                gap: 10px;
            }}
        }}
        @media (min-width: 1024px) {{
            .sb-catalog-list {{
                grid-template-columns: repeat(3, minmax(0, 1fr));
                gap: 12px;
            }}
        }}
        @media (min-width: 1280px) {{
            .sb-catalog-list {{
                grid-template-columns: repeat(4, minmax(0, 1fr));
            }}
        }}
        .sb-catalog-row {{
            position: relative;
            display: grid;
            grid-template-columns: 52px minmax(0, 1fr);
            gap: 10px;
            align-items: start;
            padding: 10px 44px 10px 10px;
            border-radius: 14px;
            border: 1px solid #e2e8f0;
            background: #fff;
            cursor: pointer;
            text-align: left;
            font: inherit;
            color: inherit;
            width: 100%;
            height: 100%;
            box-sizing: border-box;
        }}
        .sb-catalog-row:hover {{
            border-color: #cbd5e1;
            box-shadow: 0 4px 14px rgba(15, 23, 42, 0.06);
        }}
        .sb-catalog-row.is-open {{
            border-color: rgba(162, 184, 142, 0.7);
        }}
        .sb-catalog-thumb {{
            width: 52px;
            height: 52px;
            border-radius: 10px;
            object-fit: contain;
            background: #f8fafc;
            border: 1px solid #eef2f7;
        }}
        .sb-catalog-thumb-ph {{
            width: 52px;
            height: 52px;
            border-radius: 10px;
            background: #f1f5f9;
        }}
        .sb-catalog-main {{
            min-width: 0;
            padding-right: 2px;
        }}
        .sb-catalog-name {{
            font-weight: 700;
            font-size: 14px;
            line-height: 1.25;
            color: var(--navy);
            margin: 0 0 2px;
        }}
        .sb-catalog-meta {{
            font-size: 12px;
            color: var(--text-muted);
            line-height: 1.35;
            margin: 0 0 4px;
        }}
        .sb-catalog-price {{
            font-size: 13px;
            font-weight: 700;
            color: var(--navy);
            margin: 0 0 6px;
        }}
        .sb-catalog-badges {{
            display: flex;
            flex-wrap: wrap;
            gap: 5px;
        }}
        .sb-catalog-badge {{
            display: inline-block;
            font-size: 11px;
            font-weight: 700;
            padding: 2px 7px;
            border-radius: 999px;
            line-height: 1.3;
        }}
        .sb-catalog-badge.is-sale {{
            background: rgba(162, 184, 142, 0.22);
            color: #3f5d34;
        }}
        .sb-catalog-badge.is-often {{
            background: rgba(37, 99, 235, 0.1);
            color: #1d4ed8;
        }}
        .sb-catalog-badge.is-new {{
            background: #e2e8f0;
            color: #475569;
        }}
        .sb-catalog-row .sb-star-btn {{
            position: absolute;
            top: 8px;
            right: 8px;
            z-index: 5;
        }}
        .sb-catalog-expand {{
            grid-column: 1 / -1;
            margin-top: 4px;
            padding-top: 8px;
            border-top: 1px solid #eef2f7;
            font-size: 13px;
            color: #334155;
            line-height: 1.4;
        }}
        .sb-catalog-expand[hidden] {{
            display: none !important;
        }}
        .sb-catalog-flavor {{
            padding: 3px 0;
        }}
        .sb-catalog-flavor.is-sale {{
            color: #3f5d34;
            font-weight: 600;
        }}
        .sb-catalog-more {{
            display: block;
            width: 100%;
            margin: 14px 0 24px;
            padding: 12px 14px;
            border-radius: 14px;
            border: 1.5px solid #cbd5e1;
            background: #fff;
            font: inherit;
            font-weight: 700;
            color: var(--navy);
            cursor: pointer;
        }}
        @media (min-width: 640px) {{
            .sb-catalog-more {{
                grid-column: 1 / -1;
            }}
        }}
        .sb-catalog-more[hidden] {{
            display: none !important;
        }}
        .sb-catalog-empty {{
            margin-top: 18px;
            padding: 16px;
            border-radius: 14px;
            background: #f8fafc;
            border: 1px solid #e2e8f0;
            font-size: 14px;
            color: #334155;
            line-height: 1.45;
            text-align: center;
        }}
        .sb-catalog-empty a {{
            color: var(--blue);
            font-weight: 600;
        }}

        .sb-tier-chips-section {{
            max-width: var(--page-max);
            margin: 4px auto 6px;
            padding: 0 8px 0 4px;
            position: relative;
            z-index: 29;
        }}

        .sb-tier-chips-scroll {{
            position: relative;
            min-width: 0;
            display: flex;
            align-items: center;
        }}

        .sb-tier-chips-scroll.has-scroll-prev::before,
        .sb-tier-chips-scroll.has-scroll-next::after {{
            content: "";
            position: absolute;
            top: 0;
            bottom: 0;
            width: 20px;
            z-index: 2;
            pointer-events: none;
        }}

        .sb-tier-chips-scroll.has-scroll-prev::before {{
            left: 0;
            background: linear-gradient(to right, var(--bg) 15%, rgba(243, 244, 246, 0));
        }}

        .sb-tier-chips-scroll.has-scroll-next::after {{
            right: 0;
            background: linear-gradient(to left, var(--bg) 15%, rgba(243, 244, 246, 0));
        }}

        .sb-tier-chips-track {{
            overflow-x: auto;
            overflow-y: hidden;
            scroll-behavior: smooth;
            -webkit-overflow-scrolling: touch;
            scrollbar-width: none;
            flex: 1;
            min-width: 0;
        }}

        .sb-tier-chips-track::-webkit-scrollbar {{
            display: none;
        }}

        .sb-tier-chips-scroll.has-scroll-prev .sb-tier-chips-track {{
            mask-image: linear-gradient(to right, transparent 0, #000 16px);
            -webkit-mask-image: linear-gradient(to right, transparent 0, #000 16px);
            padding-left: 26px;
        }}

        .sb-tier-chips-scroll.has-scroll-next .sb-tier-chips-track {{
            mask-image: linear-gradient(to left, transparent 0, #000 16px);
            -webkit-mask-image: linear-gradient(to left, transparent 0, #000 16px);
            padding-right: 26px;
        }}

        .sb-tier-chips-scroll.has-scroll-prev.has-scroll-next .sb-tier-chips-track {{
            mask-image: linear-gradient(to right, transparent 0, #000 16px, #000 calc(100% - 16px), transparent);
            -webkit-mask-image: linear-gradient(to right, transparent 0, #000 16px, #000 calc(100% - 16px), transparent);
        }}

        .sb-tier-chips {{
            display: flex;
            flex-wrap: nowrap;
            gap: 6px;
            align-items: center;
            padding: 4px 0;
            margin: 0;
            min-width: min-content;
        }}

        .sb-tier-chips-label {{
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            color: #64748b;
            margin-right: 2px;
            flex: 0 0 auto;
        }}

        .sb-tier-chip {{
            position: relative;
            display: inline-flex;
            align-items: center;
            gap: 6px;
            border: 1px solid #111827;
            background: #ffffff;
            color: #111827;
            border-radius: 999px;
            padding: 10px 14px;
            font-size: 14px;
            font-weight: 600;
            font-family: inherit;
            cursor: pointer;
            line-height: 1.2;
            white-space: nowrap;
            flex: 0 0 auto;
            transition: background 0.12s ease, border-color 0.12s ease, color 0.12s ease, box-shadow 0.12s ease;
        }}

        .sb-tier-chip:hover {{
            background: #f9fafb;
        }}

        .sb-tier-chip.is-active {{
            background: #111827;
            border-color: #111827;
            color: #fff;
            box-shadow: inset 0 0 0 1px #111827;
        }}

        .sb-tier-chip[data-tier-filter="fire"].is-active {{
            background: linear-gradient(135deg, #ea580c, #c2410c);
            border-color: #c2410c;
            box-shadow: none;
        }}

        .sb-tier-chip[data-tier-filter="diamond"].is-active {{
            background: linear-gradient(135deg, #2563eb, #1d4ed8);
            border-color: #1d4ed8;
            box-shadow: none;
        }}

        .sb-tier-chip.sb-my-items-chip.is-active {{
            border-color: #f59e0b;
            background: #fffbeb;
            color: #b45309;
            box-shadow: inset 0 0 0 1px #f59e0b;
        }}

        @media (max-width: 640px) {{
            .sb-tier-chips-section {{
                padding: 0 6px 0 2px;
            }}

            .sb-tier-chip {{
                padding: 9px 12px;
                font-size: 13px;
            }}
        }}

        .sb-star-btn {{
            position: absolute;
            top: 10px;
            right: 10px;
            z-index: 5;
            width: 36px;
            height: 36px;
            border: none;
            border-radius: 999px;
            background: rgba(255, 255, 255, 0.92);
            box-shadow: 0 2px 8px rgba(15, 23, 42, 0.12);
            display: inline-flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            color: #94a3b8;
            padding: 0;
        }}
        .sb-star-btn:hover {{
            color: #f59e0b;
        }}
        .sb-star-btn.is-starred {{
            color: #f59e0b;
        }}
        .sb-star-icon {{
            width: 20px;
            height: 20px;
            fill: none;
            stroke: currentColor;
            stroke-width: 1.8;
            stroke-linejoin: round;
        }}
        .sb-star-btn.is-starred .sb-star-icon {{
            fill: currentColor;
            stroke: currentColor;
        }}
        .sb-last-sale-lines {{
            margin: 8px 0 0 0;
            display: grid;
            gap: 4px;
        }}
        .sb-last-sale-line {{
            font-size: 12px;
            line-height: 1.35;
            color: var(--text-muted);
        }}
        .sb-last-sale-line.is-now {{
            color: #15803d;
            font-weight: 600;
        }}
        .sb-my-items-today-line {{
            display: none;
            width: 100%;
            margin: 0 0 12px 0;
            padding: 10px 14px;
            border: 1px solid #A2B88E;
            border-left: 4px solid #A2B88E;
            border-radius: 10px;
            background: #ffffff;
            color: var(--text-main);
            font-size: 14px;
            font-weight: 700;
            font-family: inherit;
            text-align: left;
            cursor: pointer;
        }}
        .sb-my-items-today-line.is-visible {{
            display: block;
        }}
        .sb-my-items-today-line:hover {{
            background: #f6f9f2;
        }}
        /* My Items: condensed on-sale cards */
        .sb-my-items-compact .card .badge,
        .sb-my-items-compact .card .diamond-sparkles,
        .sb-my-items-compact .card .streak,
        .sb-my-items-compact .card .card-footer,
        .sb-my-items-compact .card [class*="availability"] {{
            display: none !important;
        }}
        .sb-my-items-compact .card .card-title {{
            margin-bottom: 0;
            min-height: 0;
        }}
        .sb-mi-extra {{
            display: none;
        }}
        .sb-my-items-compact .sb-mi-extra {{
            display: block;
        }}
        .sb-mi-store {{
            margin: 0;
            font-size: 12px;
            font-weight: 600;
            line-height: 1.35;
            color: #374151;
        }}
        .sb-mi-also {{
            margin: 0;
            font-size: 11px;
            line-height: 1.35;
            color: var(--text-muted);
        }}
        .sb-deal-history {{
            margin: 6px 0 0 0;
            font-size: 12px;
            color: var(--text-muted);
        }}
        .sb-deal-history[hidden] {{
            display: none;
        }}
        .sb-deal-history > summary {{
            cursor: pointer;
            font-weight: 600;
            color: var(--text-muted);
            list-style: none;
        }}
        .sb-deal-history > summary::-webkit-details-marker {{
            display: none;
        }}
        .sb-hist-chev {{
            display: inline-block;
            transition: transform 0.2s ease;
        }}
        .sb-deal-history[open] .sb-hist-chev {{
            transform: rotate(180deg);
        }}
        .sb-deal-history .sb-last-sale-lines {{
            margin-top: 6px;
        }}
        /* My Items: compact not-on-sale rows */
        .sb-my-items-offrow {{
            grid-column: 1 / -1;
            position: relative;
            display: flex;
            align-items: flex-start;
            gap: 10px;
            padding: 10px 12px;
            border-radius: 10px;
            background: #f3f4f6;
            border: 1px solid #e5e7eb;
        }}
        .sb-my-items-offrow .sb-star-btn {{
            position: static;
            flex: 0 0 auto;
            width: 32px;
            height: 32px;
            box-shadow: none;
            background: transparent;
        }}
        .sb-offrow-main {{
            flex: 1 1 auto;
            min-width: 0;
        }}
        .sb-my-items-offrow .card-title {{
            margin: 0;
            min-height: 0;
            font-size: 14px;
            font-weight: 600;
            color: #374151;
        }}
        .sb-offrow-sub {{
            margin: 2px 0 0 0;
            font-size: 12px;
            line-height: 1.35;
            color: #6b7280;
        }}
        /* Savings slide-out panel */
        .sb-savings-panel {{
            position: fixed;
            left: 12px;
            right: 12px;
            bottom: 12px;
            z-index: 42;
            max-width: 460px;
            max-height: 80vh;
            overflow-y: auto;
            margin: 0 auto;
            padding: 16px 44px 14px 16px;
            border-radius: 14px;
            background: #ffffff;
            border: 1px solid var(--border-subtle);
            border-top: 4px solid #A2B88E;
            box-shadow: 0 12px 32px rgba(15, 23, 42, 0.18);
            transform: translateY(120%);
            opacity: 0;
            pointer-events: none;
            transition: transform 0.28s ease, opacity 0.28s ease;
        }}
        .sb-savings-panel[hidden] {{
            display: none;
        }}
        .sb-savings-panel.is-on {{
            transform: translateY(0);
            opacity: 1;
            pointer-events: auto;
        }}
        .sb-savings-panel-close {{
            position: absolute;
            top: 8px;
            right: 8px;
            width: 32px;
            height: 32px;
            border: none;
            border-radius: 999px;
            background: transparent;
            color: var(--text-muted);
            font-size: 22px;
            line-height: 1;
            cursor: pointer;
        }}
        .sb-savings-panel-close:hover {{
            color: var(--text-main);
            background: #f1f5f9;
        }}
        .sb-savings-panel-headline {{
            margin: 0 0 6px 0;
            font-size: 16px;
            font-weight: 700;
            line-height: 1.35;
            color: var(--text-main);
        }}
        .sb-savings-panel-line {{
            margin: 0 0 6px 0;
            font-size: 14px;
            line-height: 1.4;
            color: #374151;
        }}
        .sb-savings-panel-qty {{
            display: flex;
            align-items: center;
            flex-wrap: wrap;
            gap: 6px;
            margin: 8px 0 10px 0;
            font-size: 13px;
            font-weight: 600;
            color: #374151;
        }}
        .sb-savings-panel-qty[hidden] {{
            display: none;
        }}
        .sb-savings-qty-btn {{
            min-width: 34px;
            padding: 5px 10px;
            border: 1px solid #A2B88E;
            border-radius: 999px;
            background: #ffffff;
            color: #374151;
            font-size: 13px;
            font-weight: 700;
            font-family: inherit;
            cursor: pointer;
        }}
        .sb-savings-qty-btn.is-active {{
            background: #A2B88E;
            color: #1f2937;
        }}
        .sb-savings-panel-how {{
            margin: 10px 0 0 0;
            border-top: 1px solid #e5e7eb;
            padding-top: 8px;
        }}
        .sb-savings-panel-how-title {{
            margin: 0 0 6px 0;
            font-size: 12px;
            font-weight: 700;
            letter-spacing: 0.03em;
            text-transform: uppercase;
            color: #6b7280;
        }}
        .sb-savings-panel-list {{
            margin: 0;
            padding: 0;
            list-style: none;
            display: grid;
            gap: 4px;
        }}
        .sb-savings-panel-list li {{
            font-size: 12px;
            line-height: 1.4;
            color: #4b5563;
        }}
        .sb-auth-line {{
            margin: 0 0 12px 0;
            font-size: 12px;
            line-height: 1.4;
            color: var(--text-muted);
            font-weight: 500;
        }}
        .sb-auth-line button {{
            background: none;
            border: none;
            padding: 0;
            margin: 0;
            color: inherit;
            font: inherit;
            text-decoration: underline;
            cursor: pointer;
        }}
        .sb-auth-line button:hover {{
            color: var(--text);
        }}
        .sb-stores-line {{
            margin: 0 0 10px 0;
            font-size: 13px;
            line-height: 1.45;
            color: var(--text-muted);
            font-weight: 500;
        }}
        .sb-stores-line button {{
            background: none;
            border: none;
            padding: 0;
            margin: 0;
            color: #047857;
            font: inherit;
            font-weight: 600;
            text-decoration: underline;
            cursor: pointer;
        }}
        .sb-stores-line button:hover {{
            color: var(--text);
        }}
        .sb-my-items-notify {{
            margin: 0 0 14px 0;
            padding: 14px 16px;
            border-radius: 12px;
            background: #fffbeb;
            border: 1px solid #f59e0b;
            box-shadow: 0 8px 20px rgba(15, 23, 42, 0.08);
        }}
        .sb-my-items-notify-title {{
            margin: 0 0 4px 0;
            font-size: 15px;
            font-weight: 700;
            color: var(--text-main);
        }}
        .sb-my-items-notify-text {{
            margin: 0 0 10px 0;
            font-size: 13px;
            line-height: 1.45;
            color: var(--text-muted);
        }}
        .sb-my-items-notify-row {{
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
        }}
        .sb-my-items-notify-email {{
            flex: 1 1 200px;
            min-width: 0;
            border: 1px solid var(--border-subtle);
            border-radius: 10px;
            padding: 10px 12px;
            font-size: 14px;
            font-family: inherit;
        }}
        .sb-my-items-notify-email:focus {{
            outline: 2px solid #f59e0b;
            outline-offset: 1px;
            border-color: #f59e0b;
        }}
        .sb-my-items-notify-submit {{
            border: none;
            border-radius: 10px;
            padding: 10px 14px;
            background: #0f172a;
            color: #fff;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            font-family: inherit;
        }}
        .sb-my-items-notify-submit:hover {{
            filter: brightness(1.08);
        }}
        .sb-my-items-notify-submit:disabled {{
            opacity: 0.6;
            cursor: default;
        }}
        .sb-my-items-notify-status {{
            margin: 8px 0 0 0;
            font-size: 13px;
            color: #15803d;
            min-height: 1.2em;
        }}
        .sb-my-items-notify-status.is-error {{
            color: #b91c1c;
        }}
        .sb-my-items-empty {{
            display: none;
            margin: 24px auto;
            max-width: 420px;
            text-align: center;
            color: var(--text-muted);
            font-size: 15px;
            line-height: 1.5;
        }}
        .sb-my-items-empty.is-visible {{
            display: block;
        }}
        body.sb-email-ask-open {{
            padding-bottom: 150px;
        }}
        .sb-email-ask {{
            position: fixed;
            left: 12px;
            right: 12px;
            bottom: 12px;
            z-index: 40;
            max-width: 420px;
            margin: 0 auto;
            padding: 14px 40px 12px 14px;
            border-radius: 14px;
            background: #ffffff;
            border: 1px solid var(--border-subtle);
            box-shadow: 0 12px 32px rgba(15, 23, 42, 0.18);
            transform: translateY(120%);
            opacity: 0;
            pointer-events: none;
            transition: transform 0.28s ease, opacity 0.28s ease;
        }}
        .sb-email-ask.is-on {{
            transform: translateY(0);
            opacity: 1;
            pointer-events: auto;
        }}
        .sb-email-ask-close {{
            position: absolute;
            top: 8px;
            right: 8px;
            width: 32px;
            height: 32px;
            border: none;
            border-radius: 999px;
            background: transparent;
            color: var(--text-muted);
            font-size: 22px;
            line-height: 1;
            cursor: pointer;
        }}
        .sb-email-ask-close:hover {{
            color: var(--text-main);
            background: #f1f5f9;
        }}
        .sb-email-ask-title {{
            margin: 0 0 10px 0;
            font-size: 14px;
            font-weight: 700;
            line-height: 1.35;
            color: var(--text-main);
            padding-right: 4px;
        }}
        .sb-email-ask-row {{
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
        }}
        .sb-email-ask-email {{
            flex: 1 1 160px;
            min-width: 0;
            border: 1px solid var(--border-subtle);
            border-radius: 10px;
            padding: 10px 12px;
            font-size: 14px;
            font-family: inherit;
        }}
        .sb-email-ask-email:focus {{
            outline: 2px solid #f59e0b;
            outline-offset: 1px;
            border-color: #f59e0b;
        }}
        .sb-email-ask-submit {{
            border: none;
            border-radius: 10px;
            padding: 10px 14px;
            background: #0f172a;
            color: #fff;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            font-family: inherit;
            white-space: nowrap;
        }}
        .sb-email-ask-submit:hover {{
            filter: brightness(1.08);
        }}
        .sb-email-ask-submit:disabled {{
            opacity: 0.6;
            cursor: default;
        }}
        .sb-email-ask-note {{
            margin: 8px 0 0 0;
            font-size: 11px;
            line-height: 1.35;
            color: var(--text-muted);
        }}
        .sb-email-ask-status {{
            margin: 6px 0 0 0;
            font-size: 13px;
            font-weight: 600;
            color: #15803d;
            min-height: 1.2em;
        }}
        .sb-email-ask-status.is-error {{
            color: #b91c1c;
        }}
        .sb-email-ask-pill {{
            position: fixed;
            right: 12px;
            bottom: 12px;
            z-index: 41;
            border: 1px solid var(--border-subtle);
            border-radius: 999px;
            background: #ffffff;
            color: var(--text-main);
            box-shadow: 0 8px 20px rgba(15, 23, 42, 0.16);
            padding: 8px 12px;
            font-size: 12px;
            font-weight: 700;
            font-family: inherit;
            line-height: 1.2;
            cursor: pointer;
            max-width: min(200px, calc(100vw - 24px));
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            transform: translateY(120%);
            opacity: 0;
            pointer-events: none;
            transition: transform 0.25s ease, opacity 0.25s ease;
        }}
        .sb-email-ask-pill.is-on {{
            transform: translateY(0);
            opacity: 1;
            pointer-events: auto;
        }}
        .sb-email-ask-pill:hover {{
            filter: brightness(0.98);
        }}
        .sb-email-ask-pill[data-mode="items"],
        .sb-email-ask-pill[data-mode="savings"] {{
            border-color: #A2B88E;
            background: #f6f9f2;
        }}
        .sb-star-toast {{
            position: fixed;
            left: 50%;
            bottom: 18px;
            z-index: 45;
            transform: translate(-50%, 120%);
            max-width: calc(100% - 24px);
            padding: 10px 14px;
            border-radius: 999px;
            background: #0f172a;
            color: #fff;
            font-size: 13px;
            font-weight: 600;
            box-shadow: 0 10px 24px rgba(15, 23, 42, 0.28);
            opacity: 0;
            pointer-events: none;
            transition: transform 0.25s ease, opacity 0.25s ease;
            text-align: center;
        }}
        .sb-star-toast.is-on {{
            transform: translate(-50%, 0);
            opacity: 1;
        }}
        @media (max-width: 640px) {{
            .sb-email-ask {{
                left: 8px;
                right: 8px;
                bottom: 8px;
                padding: 12px 36px 10px 12px;
            }}
            .sb-email-ask-title {{
                font-size: 13px;
            }}
            .sb-savings-panel {{
                left: 8px;
                right: 8px;
                bottom: 8px;
                padding: 14px 40px 12px 12px;
            }}
            .sb-email-ask-pill {{
                right: 8px;
                bottom: 8px;
                padding: 8px 11px;
                font-size: 12px;
                max-width: min(180px, calc(100vw - 16px));
            }}
        }}

        .sb-hero-tier {{
            display: inline-flex;
            align-items: center;
            gap: 4px;
            font-size: 12px;
            font-weight: 600;
            color: #475569;
            letter-spacing: 0.01em;
            border: 1px solid transparent;
            background: rgba(255, 255, 255, 0.55);
            border-radius: 999px;
            padding: 4px 9px;
            cursor: pointer;
            font-family: inherit;
        }}

        .sb-hero-tier:hover {{
            border-color: #cbd5e1;
            background: rgba(255, 255, 255, 0.9);
        }}

        .sb-hero-tier.is-active {{
            border-color: var(--navy);
            background: var(--navy);
            color: #fff;
        }}

        .sb-hero-title {{
            font-weight: 700;
            font-size: 20px;
            margin: 4px 0;
        }}

        @media (min-width: 768px) {{
            .sb-hero-title {{
                font-size: 24px;
            }}
        }}

        .sb-hero-subtitle {{
            font-size: 15px;
            font-weight: 400;
            margin: 4px 0 10px;
            color: #374151; /* same muted tone as hero freshness copy */
            line-height: 1.35;
        }}

        .sb-hero-tiers {{
            display: flex;
            flex-wrap: wrap;
            gap: 6px 12px;
            margin-top: 10px;
            align-items: center;
        }}

        .sb-hero-tiers-label {{
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            color: #64748b;
            margin-right: 2px;
        }}

        .sb-hero-tier-icon {{
            font-size: 13px;
            line-height: 1;
        }}

        .sb-hero-cta {{
            display: inline-block;
            padding: 8px 16px;
            border-radius: var(--radius-pill);
            background-color: var(--navy); /* dark navy CTA */
            color: #ffffff;
            text-decoration: none;
            font-size: 13px;
            font-weight: 600;
        }}

        .sb-hero-cta:hover {{
            filter: brightness(1.02);
        }}

        /* ============================ */
        /* SECTION 2: DEAL CARD GRID    */
        /* ============================ */

        .card-grid {{
            max-width: var(--page-max);
            margin: 0 auto;
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
            gap: 14px;
            padding-bottom: 88px;
        }}

        @media (min-width: 1100px) {{
            .card-grid {{
                grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
                gap: 16px;
            }}
        }}

        @media (max-width: 640px) {{
            .card-grid {{
                grid-template-columns: 1fr;
                gap: 12px;
            }}
        }}

        .card {{
            position: relative;
            background-color: var(--card-bg);
            border-radius: var(--radius-lg);
            border: 1px solid var(--border-subtle);
            box-shadow: var(--shadow-soft);
            padding: 10px 10px 12px 10px;
            display: flex;
            flex-direction: column;
        }}

        .card-tier-diamond {{
            position: relative;
            overflow: hidden;
            background: linear-gradient(165deg, #dbeafe 0%, #eff6ff 42%, #f8fbff 100%);
            border-color: #60a5fa;
            box-shadow:
                0 0 0 1px rgba(59, 130, 246, 0.22),
                0 10px 28px rgba(37, 99, 235, 0.16);
        }}

        /* Soft light sweep — unique delay + duration per card */
        .card-tier-diamond::before {{
            content: "";
            position: absolute;
            top: -30%;
            bottom: -30%;
            left: -40%;
            width: 52%;
            z-index: 4;
            pointer-events: none;
            background: linear-gradient(
                105deg,
                transparent 0%,
                rgba(255, 255, 255, 0.03) 38%,
                rgba(255, 255, 255, 0.16) 50%,
                rgba(147, 197, 253, 0.08) 58%,
                transparent 78%
            );
            transform: translateX(0) skewX(-14deg);
            animation: diamond-sheen var(--diamond-sheen-duration, 5s) ease-in-out infinite;
            animation-delay: var(--diamond-sheen-delay, 0s);
        }}

        @keyframes diamond-sheen {{
            0% {{
                transform: translateX(-30%) skewX(-14deg);
                opacity: 0;
            }}
            8% {{
                opacity: 0.85;
            }}
            28% {{
                opacity: 0.85;
            }}
            40% {{
                transform: translateX(280%) skewX(-14deg);
                opacity: 0;
            }}
            100% {{
                transform: translateX(280%) skewX(-14deg);
                opacity: 0;
            }}
        }}

        .diamond-sparkles {{
            position: absolute;
            inset: 0;
            pointer-events: none;
            z-index: 5;
        }}

        .sparkle {{
            position: absolute;
            opacity: 0;
            transform: scale(0.1);
            display: block;
            line-height: 0;
            animation: diamond-twinkle-a 7s linear infinite;
        }}

        .sparkle-svg {{
            display: block;
            width: 100%;
            height: 100%;
            overflow: visible;
        }}

        .sparkle-shape {{
            fill: #ffffff;
            stroke: #1e40af;
            stroke-width: 4;
            stroke-linejoin: miter;
            stroke-miterlimit: 8;
            paint-order: stroke fill;
            filter: drop-shadow(0 0 4px rgba(37, 99, 235, 0.9));
        }}

        @keyframes diamond-twinkle-a {{
            0%, 100% {{
                opacity: 0;
                transform: scale(0.05);
            }}
            8% {{
                opacity: 0.55;
                transform: scale(calc(var(--sparkle-peak, 1.15) * 0.45));
            }}
            14% {{
                opacity: 1;
                transform: scale(var(--sparkle-peak, 1.15));
            }}
            20% {{
                opacity: 0.5;
                transform: scale(calc(var(--sparkle-peak, 1.15) * 0.45));
            }}
            28% {{
                opacity: 0;
                transform: scale(0.05);
            }}
            55% {{
                opacity: 0;
                transform: scale(0.05);
            }}
            62% {{
                opacity: 0.7;
                transform: scale(calc(var(--sparkle-peak, 1.15) * 0.5));
            }}
            68% {{
                opacity: 1;
                transform: scale(calc(var(--sparkle-peak, 1.15) * 0.95));
            }}
            74% {{
                opacity: 0;
                transform: scale(0.05);
            }}
        }}

        @keyframes diamond-twinkle-b {{
            0%, 100% {{
                opacity: 0;
                transform: scale(0.05);
            }}
            5% {{
                opacity: 0.5;
                transform: scale(calc(var(--sparkle-peak, 1.1) * 0.45));
            }}
            10% {{
                opacity: 1;
                transform: scale(var(--sparkle-peak, 1.1));
            }}
            15% {{
                opacity: 0.45;
                transform: scale(calc(var(--sparkle-peak, 1.1) * 0.45));
            }}
            20% {{
                opacity: 0;
                transform: scale(0.05);
            }}
            40% {{
                opacity: 0;
                transform: scale(0.05);
            }}
            46% {{
                opacity: 0.55;
                transform: scale(calc(var(--sparkle-peak, 1.1) * 0.45));
            }}
            52% {{
                opacity: 1;
                transform: scale(calc(var(--sparkle-peak, 1.1) * 0.95));
            }}
            58% {{
                opacity: 0;
                transform: scale(0.05);
            }}
            78% {{
                opacity: 0;
                transform: scale(0.05);
            }}
            84% {{
                opacity: 0.65;
                transform: scale(calc(var(--sparkle-peak, 1.1) * 0.5));
            }}
            90% {{
                opacity: 1;
                transform: scale(calc(var(--sparkle-peak, 1.1) * 0.9));
            }}
            96% {{
                opacity: 0;
                transform: scale(0.05);
            }}
        }}

        @keyframes diamond-twinkle-c {{
            0%, 100% {{
                opacity: 0;
                transform: scale(0.05);
            }}
            18% {{
                opacity: 0;
                transform: scale(0.05);
            }}
            24% {{
                opacity: 0.55;
                transform: scale(calc(var(--sparkle-peak, 1.2) * 0.45));
            }}
            30% {{
                opacity: 1;
                transform: scale(var(--sparkle-peak, 1.2));
            }}
            36% {{
                opacity: 0.45;
                transform: scale(calc(var(--sparkle-peak, 1.2) * 0.45));
            }}
            42% {{
                opacity: 0;
                transform: scale(0.05);
            }}
            68% {{
                opacity: 0;
                transform: scale(0.05);
            }}
            74% {{
                opacity: 0.7;
                transform: scale(calc(var(--sparkle-peak, 1.2) * 0.5));
            }}
            80% {{
                opacity: 1;
                transform: scale(calc(var(--sparkle-peak, 1.2) * 0.92));
            }}
            86% {{
                opacity: 0;
                transform: scale(0.05);
            }}
        }}

        @keyframes diamond-twinkle-d {{
            0%, 100% {{
                opacity: 0;
                transform: scale(0.05);
            }}
            4% {{
                opacity: 0.5;
                transform: scale(calc(var(--sparkle-peak, 1) * 0.4));
            }}
            8% {{
                opacity: 1;
                transform: scale(calc(var(--sparkle-peak, 1) * 0.95));
            }}
            12% {{
                opacity: 0;
                transform: scale(0.05);
            }}
            28% {{
                opacity: 0;
                transform: scale(0.05);
            }}
            32% {{
                opacity: 0.55;
                transform: scale(calc(var(--sparkle-peak, 1) * 0.4));
            }}
            36% {{
                opacity: 1;
                transform: scale(calc(var(--sparkle-peak, 1) * 0.9));
            }}
            40% {{
                opacity: 0;
                transform: scale(0.05);
            }}
            58% {{
                opacity: 0;
                transform: scale(0.05);
            }}
            62% {{
                opacity: 0.6;
                transform: scale(calc(var(--sparkle-peak, 1) * 0.4));
            }}
            66% {{
                opacity: 1;
                transform: scale(calc(var(--sparkle-peak, 1) * 0.92));
            }}
            70% {{
                opacity: 0;
                transform: scale(0.05);
            }}
            86% {{
                opacity: 0;
                transform: scale(0.05);
            }}
            90% {{
                opacity: 0.65;
                transform: scale(calc(var(--sparkle-peak, 1) * 0.45));
            }}
            94% {{
                opacity: 1;
                transform: scale(calc(var(--sparkle-peak, 1) * 0.88));
            }}
            98% {{
                opacity: 0;
                transform: scale(0.05);
            }}
        }}

        @media (prefers-reduced-motion: reduce) {{
            .diamond-sparkles {{
                display: none;
            }}
            .card-tier-diamond::before {{
                animation: none;
                opacity: 0;
            }}
        }}

        .card-tier-fire {{
            background-color: #ffedd5;
            border-color: #f9a03f;
        }}

        .card-tier-strong {{
            background-color: #faf9f6;
            border-color: #e5dfd0;
        }}

        .card-tier-sale {{
            background-color: #fafafa;
            border-color: #e8eaed;
        }}

        .card-image-wrap {{
            position: relative;
            padding: 8px 4px;
            min-height: 130px;
        }}

        .card-carousel {{
            position: relative;
            width: 100%;
            display: grid;
            grid-template-columns: 30px minmax(0, 1fr) 30px;
            grid-template-rows: auto auto;
            align-items: center;
            column-gap: 2px;
            padding: 0;
            z-index: 1;
        }}

        .card-carousel--single {{
            grid-template-columns: minmax(0, 1fr);
        }}

        .card-carousel-stage {{
            grid-column: 2;
            grid-row: 1;
            position: relative;
            height: 118px;
            display: flex;
            align-items: center;
            justify-content: center;
            overflow: hidden;
        }}

        .card-carousel--single .card-carousel-stage {{
            grid-column: 1;
        }}

        .card-carousel-slide {{
            position: absolute;
            left: 50%;
            top: 50%;
            width: 62%;
            max-width: 200px;
            transition: transform 0.28s ease, opacity 0.28s ease;
            transform: translate(-50%, -50%) scale(0.78);
            opacity: 0;
            pointer-events: none;
            z-index: 1;
        }}

        .card-carousel-slide.is-active {{
            transform: translate(-50%, -50%) scale(1);
            opacity: 1;
            pointer-events: auto;
            z-index: 2;
        }}

        .card-carousel-slide.is-prev {{
            transform: translate(calc(-50% - 58%), -50%) scale(0.82);
            opacity: 0.42;
            z-index: 1;
        }}

        .card-carousel-slide.is-next {{
            transform: translate(calc(-50% + 58%), -50%) scale(0.82);
            opacity: 0.42;
            z-index: 1;
        }}

        .card-carousel-slide.is-hidden {{
            opacity: 0;
            z-index: 0;
        }}

        .card-carousel-link {{
            display: flex;
            align-items: center;
            justify-content: center;
            width: 100%;
            height: 100%;
            text-decoration: none;
        }}

        .card-image {{
            max-width: 100%;
            max-height: 110px;
            object-fit: contain;
            display: block;
        }}

        .card-carousel-prev,
        .card-carousel-next {{
            position: relative;
            top: auto;
            transform: none;
            justify-self: center;
            align-self: center;
            z-index: 5;
            width: 26px;
            height: 26px;
            border: 1px solid var(--border-subtle);
            border-radius: 999px;
            background: rgba(255, 255, 255, 0.95);
            color: var(--text-main);
            font-size: 18px;
            line-height: 1;
            padding: 0;
            cursor: pointer;
            box-shadow: 0 1px 4px rgba(15, 23, 42, 0.12);
        }}

        .card-carousel-prev {{
            grid-column: 1;
            grid-row: 1;
        }}

        .card-carousel-next {{
            grid-column: 3;
            grid-row: 1;
        }}

        .card-carousel-prev:hover,
        .card-carousel-next:hover {{
            filter: brightness(0.98);
        }}

        .card-carousel-caption {{
            grid-column: 1 / -1;
            grid-row: 2;
            margin-top: 4px;
            text-align: center;
            font-size: 11px;
            font-weight: 600;
            color: var(--text-muted);
            line-height: 1.25;
            padding: 0 4px;
            min-height: 14px;
        }}

        @media (max-width: 640px) {{
            .card-carousel-slide.is-prev {{
                transform: translate(calc(-50% - 48%), -50%) scale(0.78);
            }}

            .card-carousel-slide.is-next {{
                transform: translate(calc(-50% + 48%), -50%) scale(0.78);
            }}
        }}

        .pack-pill {{
            position: absolute;
            bottom: 6px;
            right: 8px;
            z-index: 12;
            background-color: #111827;
            color: #ffffff;
            border-radius: 999px;
            padding: 2px 8px;
            font-size: 11px;
            display: inline-flex;
            align-items: center;
            gap: 2px;
        }}

        .pack-count {{
            font-weight: 700;
            text-transform: uppercase;
            font-size: 11px;
        }}

        .pack-unit {{
            text-transform: uppercase;
            font-size: 10px;
        }}

        .badge {{
            position: absolute;
            top: 10px;
            left: 10px;
            z-index: 13;
            border-radius: var(--radius-pill);
            padding: 5px 12px;
            font-size: 13px;
            font-weight: 700;
            letter-spacing: 0.01em;
            display: inline-flex;
            align-items: center;
            gap: 5px;
            background-color: #ffffff;
            border: 1.5px solid #e5e7eb;
            box-shadow: 0 2px 6px rgba(15, 23, 42, 0.12);
        }}

        .badge-strong {{
            color: var(--orange);
            border-color: #fdba74;
        }}

        .badge-fire-pill {{
            overflow: hidden;
        }}

        .fire-pill-label {{
            position: relative;
            z-index: 2;
        }}

        .fire-pill-flames {{
            position: absolute;
            left: 0;
            right: 0;
            bottom: 0;
            height: 16px;
            pointer-events: none;
            z-index: 1;
            opacity: 0;
            transform: scaleY(0.15) translateY(3px);
            transform-origin: center bottom;
            animation: fire-pill-flames-rise 9s linear infinite;
        }}

        .fire-pill-flames-svg {{
            display: block;
            width: 100%;
            height: 100%;
        }}

        .fire-pill-spark {{
            opacity: 0;
            transform-origin: center center;
            animation: fire-pill-spark-pop 9s linear infinite;
            animation-delay: inherit;
        }}

        @keyframes fire-pill-flames-rise {{
            0%, 48%, 100% {{
                opacity: 0;
                transform: scaleY(0.12) translateY(4px);
            }}
            54% {{
                opacity: 0.55;
                transform: scaleY(0.5) translateY(2px);
            }}
            60% {{
                opacity: 1;
                transform: scaleY(0.95) translateY(0);
            }}
            72% {{
                opacity: 1;
                transform: scaleY(1.08) translateY(-1px);
            }}
            84% {{
                opacity: 0.85;
                transform: scaleY(0.85) translateY(0);
            }}
            92% {{
                opacity: 0.45;
                transform: scaleY(0.45) translateY(2px);
            }}
            97% {{
                opacity: 0;
                transform: scaleY(0.1) translateY(4px);
            }}
        }}

        @keyframes fire-pill-spark-pop {{
            0%, 56%, 100% {{
                opacity: 0;
                transform: translateY(6px) scale(0.4);
            }}
            62% {{
                opacity: 0.85;
                transform: translateY(0) scale(1);
            }}
            74% {{
                opacity: 0.7;
                transform: translateY(-2px) scale(0.95);
            }}
            86% {{
                opacity: 0.35;
                transform: translateY(-5px) scale(0.7);
            }}
            94% {{
                opacity: 0;
                transform: translateY(-7px) scale(0.35);
            }}
        }}

        @media (prefers-reduced-motion: reduce) {{
            .fire-pill-flames,
            .fire-pill-spark {{
                animation: none;
                opacity: 0;
            }}
        }}

        .badge-regular {{
            background-color: #fefce8;
            color: #854d0e;
            border: 1px solid #facc15;
        }}

        .badge-elite {{
            color: #1e3a8a;
            border-color: #60a5fa;
            background: linear-gradient(180deg, #ffffff 0%, #dbeafe 100%);
            box-shadow: 0 0 0 1px rgba(59, 130, 246, 0.2), 0 2px 8px rgba(37, 99, 235, 0.2);
            animation: diamond-badge-glow 2.8s ease-in-out infinite;
            animation-delay: var(--diamond-badge-delay, 0s);
        }}

        @keyframes diamond-badge-glow {{
            0%, 100% {{
                box-shadow: 0 0 0 1px rgba(59, 130, 246, 0.2), 0 2px 8px rgba(37, 99, 235, 0.18);
            }}
            50% {{
                box-shadow: 0 0 0 1px rgba(59, 130, 246, 0.45), 0 3px 12px rgba(37, 99, 235, 0.35);
            }}
        }}

        .badge-protein {{
            color: #854d0e;
            border-color: #facc15;
        }}

        .badge-everyday {{
            color: #6b7280;
            border-color: #d1d5db;
        }}

        .badge-bundle {{
            color: #0f766e;
            border-color: #5eead4;
            background: linear-gradient(180deg, #ffffff 0%, #ccfbf1 100%);
            max-width: 11.5rem;
            white-space: normal;
            line-height: 1.2;
            text-align: left;
        }}

        .card-tier-bundle {{
            /* Everyday multipack saving — still in grid, not a deal tier */
        }}

        .card-content {{
            padding: 4px 6px 12px 6px;
            display: flex;
            flex-direction: column;
            gap: 6px;
            flex: 1;
        }}

        .card-meta-row {{
            display: flex;
            align-items: center;
            gap: 6px;
            font-size: 11px;
        }}

        .retailer-pill {{
            padding: 3px 8px;
            border-radius: var(--radius-pill);
            font-size: 11px;
            font-weight: 600;
        }}

        .retailer-walmart {{
            background-color: #0071CE;
            color: #F9C80E;
        }}

        .retailer-kroger {{
            background-color: #003087;
            color: #ffffff;
        }}

        .retailer-target {{
            background-color: #CC0000;
            color: #ffffff;
        }}

        .retailer-meijer {{
            background-color: #004F91;
            color: #E31837;
            font-weight: 700;
        }}

        .retailer-harris-teeter {{
            background-color: #8E2344;
            color: #ffffff;
        }}

        .retailer-generic {{
            background-color: #6b7280;
            color: #ffffff;
        }}

        .meta-category {{
            color: var(--text-muted);
        }}

        .card-title {{
            font-size: 14px;
            font-weight: 600;
            line-height: 1.25;
            min-height: 34px;
            margin-bottom: -20px;
        }}

        .card-pricing {{
            display: flex;
            align-items: baseline;
            gap: 6px;
            font-size: 13px;
            margin-top: 0;
            margin-bottom: 0;
        }}

        .old-price {{
            color: #ef4444;
            text-decoration: line-through;
        }}

        .new-price {{
            color: var(--text-main);
            font-weight: 700;
        }}

        .percent-off {{
            color: var(--green);
            font-size: 18px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.03em;
            line-height: 1;
        }}

        .card-footer {{
            display: flex;
            flex-wrap: wrap;
            justify-content: space-between;
            align-items: center;
            gap: 4px;
            margin-top: 2px;
            font-size: 11px;
        }}

        .card-content > .availability,
        .sb-savings-tile > .availability {{
            font-size: 11px;
            color: var(--text-muted);
            line-height: 1.3;
            margin-top: 2px;
        }}
        .availability.check {{
            font-style: italic;
            color: var(--text-muted);
        }}

        .streak {{
            color: var(--text-muted);
            font-style: italic;
        }}

        .view-button {{
            margin-top: auto;
            display: block;
            text-align: center;
            padding: 7px 10px;
            border-radius: 12px;
            font-size: 13px;
            font-weight: 600;
            text-decoration: none;
        }}

        .view-walmart {{
            background-color: #0071CE;
            color: #F9C80E;
        }}

        .view-kroger {{
            background-color: #003087;
            color: #ffffff;
        }}

        .view-target {{
            background-color: #CC0000;
            color: #ffffff;
        }}

        .view-meijer {{
            background-color: #004F91;
            color: #E31837;
            font-weight: 700;
        }}

        .view-harris-teeter {{
            background-color: #8E2344;
            color: #ffffff;
        }}

        .view-generic {{
            background-color: #4b5563;
            color: #ffffff;
        }}

        .view-button:hover {{
            filter: brightness(0.95);
        }}

        /* ============================ */
        /* FLAVOR EXPANDABLE LIST       */
        /* ============================ */

        .flavor-info {{
            font-size: 12px;
            color: var(--text-main);
            margin-top: 0;
            margin-bottom: 0;
            line-height: 1.4;
        }}

        .flavor-label {{
            color: var(--text-muted);
        }}

        .flavor-sample {{
            color: var(--text-main);
        }}

        .flavor-expand-link {{
            background: none;
            border: none;
            color: var(--blue);
            cursor: pointer;
            font-size: 12px;
            padding: 0;
            margin-left: 4px;
            text-decoration: underline;
            font-weight: 600;
        }}

        .flavor-expand-link:hover {{
            color: #1e40af;
        }}

        .flavor-list-expanded {{
            margin-top: 6px;
            display: flex;
            flex-direction: column;
            gap: 4px;
        }}

        .flavor-link {{
            color: var(--blue);
            text-decoration: none;
            font-size: 12px;
            padding: 2px 0;
        }}

        .flavor-link:hover {{
            color: #1e40af;
            text-decoration: underline;
        }}

        /* ============================ */
        /* SECTION 3: INFO SECTIONS     */
        /* ============================ */

        .sb-info {{
            max-width: var(--page-max);
            margin: 18px auto 0;
        }}

        .sb-info-card {{
            background: #ffffff;
            border: 1px solid var(--border-subtle);
            border-radius: var(--radius-lg);
            box-shadow: var(--shadow-soft);
            padding: 14px 14px;
            margin-top: 14px;
        }}

        .sb-info-title {{
            font-size: 16px;
            font-weight: 800;
            margin: 0 0 6px 0;
        }}

        .sb-info-text {{
            margin: 0;
            color: var(--text-main);
            line-height: 1.55;
            font-size: 14px;
        }}

        .sb-subscribe-row {{
            margin-top: 10px;
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            align-items: center;
        }}

        .sb-email {{
            flex: 1;
            min-width: 220px;
            padding: 10px 12px;
            border-radius: 12px;
            border: 1px solid var(--border-subtle);
            font-size: 14px;
        }}

        .sb-subscribe-btn {{
            padding: 10px 14px;
            border-radius: 12px;
            border: none;
            background: var(--navy);
            color: #ffffff;
            font-weight: 800;
            cursor: pointer;
            font-size: 14px;
        }}

        .sb-subscribe-btn:hover {{
            filter: brightness(1.03);
        }}

.sb-subscribe-btn{{
  display: inline-flex;
  align-items: center;
  justify-content: center;
  text-decoration: none;
}}

.sb-subscribe-alt{{
  font-size: 13px;
  color: var(--text-muted);
  text-decoration: underline;
  font-weight: 600;
}}

        .sb-note {{
            margin-top: 10px;
            font-size: 13px;
            color: var(--text-muted);
            line-height: 1.45;
        }}

        .sb-site-footer {{
            max-width: var(--page-max);
            margin: 0 auto;
            padding: 8px 16px 28px;
        }}

        .sb-site-footer p {{
            margin: 0;
            font-size: 12px;
            line-height: 1.5;
            color: var(--text-muted);
            max-width: 52rem;
        }}

        @media (max-width: 640px) {{
            .page {{
                padding-top: 8px;
            }}
        }}

        /* ============================ */
        /* WELCOME / PREFS EXPERIENCE   */
        /* ============================ */
        .sb-welcome {{
            position: fixed;
            inset: 0;
            z-index: 10050;
            display: flex;
            align-items: stretch;
            justify-content: center;
            opacity: 0;
            pointer-events: none;
            transition: opacity 0.35s ease;
        }}
        .sb-welcome.open {{
            opacity: 1;
            pointer-events: auto;
        }}
        .sb-welcome-backdrop {{
            position: absolute;
            inset: 0;
            /* Frosted glass over the live deals page (unfiltered on first step) */
            background: rgba(243, 244, 246, 0.35);
            -webkit-backdrop-filter: blur(18px) saturate(1.15);
            backdrop-filter: blur(18px) saturate(1.15);
        }}
        @supports not ((backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px))) {{
            .sb-welcome-backdrop {{
                background: rgba(15, 23, 42, 0.45);
            }}
        }}
        .sb-welcome-panel {{
            position: relative;
            z-index: 1;
            display: flex;
            flex-direction: column;
            width: min(620px, 100%);
            /* Never taller than the visible viewport (iPhone Safari toolbar included). */
            height: 100vh;
            height: 100dvh;
            max-height: 100vh;
            max-height: 100dvh;
            margin: 0 auto;
            overflow: hidden;
            padding: 0;
            padding-bottom: env(safe-area-inset-bottom, 0px);
            border-radius: 0;
            background:
                linear-gradient(180deg, rgba(255,255,255,0.98), rgba(239, 246, 255, 0.94));
            border: 1px solid rgba(37, 99, 235, 0.12);
            box-shadow: 0 30px 80px rgba(15, 23, 42, 0.22);
            font-family: "Outfit", system-ui, sans-serif;
            transform: translateY(18px) scale(0.98);
            transition: transform 0.4s cubic-bezier(0.22, 1, 0.36, 1);
            box-sizing: border-box;
        }}
        @media (min-width: 560px) {{
            .sb-welcome-panel {{
                margin: auto 16px;
                height: min(92vh, 92dvh, 780px);
                max-height: min(92vh, 92dvh, 780px);
                border-radius: 24px;
            }}
        }}
        .sb-welcome.open .sb-welcome-panel {{
            transform: translateY(0) scale(1);
        }}
        .sb-welcome-close {{
            position: absolute;
            top: max(10px, env(safe-area-inset-top, 0px));
            right: 12px;
            z-index: 5;
            width: 40px;
            height: 40px;
            border: none;
            border-radius: 999px;
            background: rgba(15, 23, 42, 0.06);
            color: #334155;
            font-size: 26px;
            line-height: 1;
            cursor: pointer;
            display: grid;
            place-items: center;
            padding: 0;
        }}
        .sb-welcome-close:hover {{
            background: rgba(15, 23, 42, 0.12);
            color: var(--navy);
        }}
        .sb-welcome-panel-header {{
            flex: 0 0 auto;
            padding: 20px 52px 8px 22px;
            padding-top: max(20px, calc(env(safe-area-inset-top, 0px) + 8px));
        }}
        .sb-welcome-panel-scroll {{
            flex: 1 1 auto;
            min-height: 0;
            overflow-x: hidden;
            overflow-y: auto;
            -webkit-overflow-scrolling: touch;
            overscroll-behavior: contain;
            padding: 4px 22px 16px;
        }}
        .sb-welcome-panel-footer {{
            flex: 0 0 auto;
            padding: 12px 22px;
            padding-bottom: max(14px, env(safe-area-inset-bottom, 0px));
            border-top: 1px solid rgba(15, 23, 42, 0.08);
            background: rgba(255, 255, 255, 0.96);
        }}
        .sb-welcome-panel-footer .sb-welcome-actions,
        .sb-welcome-panel-footer .sb-prefs-actions {{
            margin-top: 0;
        }}
        .sb-welcome-brand {{
            font-family: "Fraunces", Georgia, serif;
            font-weight: 700;
            font-size: clamp(2rem, 6vw, 2.75rem);
            line-height: 1.05;
            letter-spacing: -0.03em;
            color: var(--navy);
            margin: 0 0 10px;
        }}
        .sb-welcome-brand span {{
            color: var(--blue);
        }}
        .sb-welcome-kicker {{
            display: inline-block;
            font-size: 12px;
            font-weight: 600;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            color: var(--green);
            margin-bottom: 8px;
        }}
        .sb-welcome-lead {{
            margin: 0 0 22px;
            font-size: 1.05rem;
            line-height: 1.45;
            color: #334155;
            max-width: 28ch;
        }}
        .sb-welcome-step {{
            display: none;
            flex: 1 1 auto;
            min-height: 0;
            flex-direction: column;
            overflow: hidden;
            animation: sbWelcomeIn 0.4s ease both;
        }}
        .sb-welcome-step.is-active {{
            display: flex;
        }}
        .sb-welcome-step .sb-welcome-panel-scroll {{
            flex: 1 1 auto;
        }}
        .sb-welcome-step .sb-welcome-panel-footer {{
            flex: 0 0 auto;
        }}
        @keyframes sbWelcomeIn {{
            from {{ opacity: 0; transform: translateY(10px); }}
            to {{ opacity: 1; transform: translateY(0); }}
        }}
        .sb-welcome-step-title {{
            font-family: "Fraunces", Georgia, serif;
            font-size: 1.45rem;
            margin: 0 0 6px;
            color: var(--navy);
        }}
        .sb-welcome-step-text {{
            margin: 0 0 16px;
            color: var(--text-muted);
            font-size: 0.95rem;
            line-height: 1.4;
        }}
        .sb-welcome-chips {{
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 10px;
            margin-bottom: 12px;
        }}
        @media (min-width: 560px) {{
            .sb-welcome-chips {{
                grid-template-columns: repeat(3, minmax(0, 1fr));
            }}
        }}
        .sb-welcome-chip {{
            appearance: none;
            border: 1.5px solid #cbd5e1;
            background: #fff;
            color: var(--navy);
            border-radius: 16px;
            padding: 12px 8px 10px;
            font: inherit;
            font-weight: 600;
            font-size: 12px;
            line-height: 1.2;
            cursor: pointer;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: flex-start;
            gap: 8px;
            min-height: 108px;
            text-align: center;
            transition: transform 0.15s ease, background 0.15s ease, border-color 0.15s ease, box-shadow 0.15s ease;
        }}
        .sb-welcome-chip:hover {{
            transform: translateY(-2px);
            border-color: var(--blue);
            box-shadow: 0 8px 18px rgba(37, 99, 235, 0.12);
        }}
        .sb-welcome-chip.is-on {{
            background: rgba(37, 99, 235, 0.08);
            border-color: var(--blue);
            box-shadow: 0 0 0 2px rgba(37, 99, 235, 0.22);
            color: var(--navy);
        }}
        .sb-welcome-chip-icon {{
            width: 52px;
            height: 52px;
            flex: 0 0 auto;
            display: grid;
            place-items: center;
            border-radius: 12px;
            overflow: hidden;
            background: #fff;
        }}
        .sb-welcome-chip-icon.is-logo {{
            width: 100%;
            height: 52px;
            border: 1px solid #e2e8f0;
        }}
        .sb-welcome-chip-icon.is-logo.is-kroger {{
            background: #0055A5;
            border-color: #00478a;
        }}
        .sb-welcome-chip-icon.is-logo.is-walmart {{
            background: #0071CE;
            border-color: #005da8;
            width: 52px;
            height: 52px;
        }}
        .sb-welcome-chip-icon.is-logo.is-kroger img,
        .sb-welcome-chip-icon.is-logo.is-meijer img,
        .sb-welcome-chip-icon.is-logo.is-walmart img {{
            width: auto;
            height: auto;
            max-width: 78%;
            max-height: 70%;
            padding: 0;
            margin: auto;
        }}
        .sb-welcome-chip-icon.is-logo.is-kroger img {{
            mix-blend-mode: screen;
            max-width: 76%;
            max-height: 68%;
        }}
        .sb-welcome-chip-icon.is-logo.is-walmart img {{
            max-width: 68%;
            max-height: 68%;
        }}
        .sb-welcome-chip-icon.is-logo.is-category {{
            width: 52px;
            height: 52px;
            background: #fff;
            border-color: #e2e8f0;
        }}
        .sb-welcome-chip-icon.is-logo.is-category img {{
            width: auto;
            height: auto;
            max-width: 88%;
            max-height: 88%;
            padding: 0;
            margin: auto;
        }}
        .sb-welcome-chip-icon svg {{
            width: 52px;
            height: 52px;
            display: block;
        }}
        .sb-welcome-chip-icon img {{
            width: 100%;
            height: 100%;
            display: block;
            object-fit: contain;
            padding: 5px 8px;
            box-sizing: border-box;
        }}
        .sb-welcome-chip-label {{
            display: block;
            max-width: 100%;
            font-size: 12px;
            font-weight: 600;
            letter-spacing: -0.01em;
            color: inherit;
        }}
        .sb-welcome-preview {{
            min-height: 1.25em;
            margin: 0 0 16px;
            font-size: 13px;
            font-weight: 600;
            color: var(--green);
        }}
        .sb-welcome-actions {{
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            align-items: center;
            margin-top: 8px;
        }}
        .sb-welcome-btn {{
            appearance: none;
            border: none;
            border-radius: 14px;
            padding: 12px 18px;
            font: inherit;
            font-weight: 700;
            font-size: 15px;
            cursor: pointer;
            background: var(--blue);
            color: #fff;
            transition: filter 0.15s ease, transform 0.15s ease;
        }}
        .sb-welcome-btn:hover {{
            filter: brightness(1.06);
            transform: translateY(-1px);
        }}
        .sb-welcome-btn:disabled {{
            opacity: 0.45;
            cursor: not-allowed;
            transform: none;
        }}
        .sb-welcome-btn-ghost {{
            background: transparent;
            color: var(--text-muted);
            font-weight: 600;
            padding: 10px 8px;
        }}
        .sb-welcome-btn-ghost:hover {{
            color: var(--blue);
            filter: none;
            transform: none;
        }}
        .sb-welcome-email-row {{
            display: flex;
            flex-direction: column;
            gap: 10px;
            margin: 4px 0 10px;
            width: 100%;
        }}
        .sb-welcome-email {{
            width: 100%;
            box-sizing: border-box;
            padding: 14px 16px;
            border-radius: 14px;
            border: 1.5px solid #cbd5e1;
            font: inherit;
            /* 16px+ avoids iOS zoom-on-focus that hides typed text */
            font-size: 16px;
            line-height: 1.35;
            color: var(--navy);
            background: #ffffff;
            -webkit-appearance: none;
            appearance: none;
        }}
        .sb-welcome-email:focus {{
            outline: 2px solid rgba(37, 99, 235, 0.35);
            border-color: var(--blue);
        }}
        .sb-welcome-email-submit {{
            width: 100%;
            box-sizing: border-box;
            padding: 14px 16px;
            border: none;
            border-radius: 14px;
            background: #0f172a;
            color: #ffffff;
            font: inherit;
            font-size: 16px;
            font-weight: 700;
            cursor: pointer;
        }}
        .sb-welcome-email-submit:hover {{
            background: #1e293b;
        }}
        .sb-welcome-email-submit:disabled {{
            opacity: 0.55;
            cursor: not-allowed;
        }}
        @media (min-width: 520px) {{
            .sb-welcome-email-row {{
                flex-direction: row;
                align-items: stretch;
            }}
            .sb-welcome-email {{
                flex: 1;
                min-width: 0;
            }}
            .sb-welcome-email-submit {{
                width: auto;
                flex: 0 0 auto;
                padding-left: 22px;
                padding-right: 22px;
            }}
        }}
        .sb-welcome-note {{
            margin: 0 0 12px;
            font-size: 12px;
            color: var(--text-muted);
            line-height: 1.4;
        }}
        .sb-welcome-edit-hint {{
            margin: 10px 0 0;
            font-size: 12px;
            color: var(--text-muted);
            line-height: 1.4;
            text-align: center;
        }}
        .sb-welcome.is-edit .sb-welcome-progress {{
            display: none;
        }}
        .sb-prefs-section {{
            margin: 0 0 18px;
        }}
        .sb-prefs-section-title {{
            font-family: "Fraunces", Georgia, serif;
            font-size: 1.2rem;
            margin: 0 0 6px;
            color: var(--navy);
        }}
        .sb-prefs-section-text {{
            margin: 0 0 12px;
            font-size: 13px;
            color: var(--text-muted);
            line-height: 1.4;
        }}
        .sb-prefs-toggle {{
            display: flex;
            align-items: center;
            gap: 10px;
            font-size: 14px;
            font-weight: 600;
            color: var(--navy);
            cursor: pointer;
            user-select: none;
        }}
        .sb-prefs-toggle input {{
            width: 18px;
            height: 18px;
            accent-color: #A2B88E;
            cursor: pointer;
        }}
        .sb-prefs-signin-note {{
            margin: 0 0 10px;
            font-size: 13px;
            color: var(--text-muted);
            line-height: 1.45;
        }}
        .sb-prefs-actions {{
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            gap: 14px;
            margin-top: 8px;
        }}
        .sb-prefs-cancel {{
            background: none;
            border: none;
            padding: 0;
            margin: 0;
            font: inherit;
            font-size: 13px;
            font-weight: 600;
            color: var(--text-muted);
            text-decoration: underline;
            cursor: pointer;
        }}
        .sb-prefs-cancel:hover {{
            color: var(--text);
        }}
        .sb-welcome-status {{
            min-height: 1.2em;
            margin: 0 0 8px;
            font-size: 13px;
            font-weight: 600;
            color: var(--green);
        }}
        .sb-welcome-status.is-error {{
            color: var(--red);
        }}
        .sb-welcome-progress {{
            display: flex;
            gap: 6px;
            margin: 0;
        }}
        .sb-welcome-dot {{
            width: 8px;
            height: 8px;
            border-radius: 999px;
            background: #cbd5e1;
            transition: background 0.2s ease, width 0.2s ease;
        }}
        .sb-welcome-dot.is-on {{
            background: var(--green);
            width: 22px;
        }}
        .sb-welcome-other {{
            margin: 0 0 14px;
            padding: 12px 14px;
            border-radius: 14px;
            background: rgba(37, 99, 235, 0.06);
            border: 1px solid rgba(37, 99, 235, 0.15);
        }}
        .sb-welcome-other-label {{
            display: block;
            font-size: 13px;
            font-weight: 700;
            color: var(--navy);
            margin-bottom: 6px;
        }}
        .sb-welcome-other-input {{
            width: 100%;
            padding: 10px 12px;
            border-radius: 12px;
            border: 1.5px solid #cbd5e1;
            font: inherit;
            font-size: 14px;
            box-sizing: border-box;
        }}
        .sb-welcome-other-input:focus {{
            outline: 2px solid rgba(37, 99, 235, 0.35);
            border-color: var(--blue);
        }}
        .sb-welcome-other-hint {{
            margin: 6px 0 0;
            font-size: 12px;
            color: var(--text-muted);
            line-height: 1.35;
        }}
        body.sb-welcome-lock {{
            overflow: hidden;
        }}
        body.sb-welcome-lock .sb-email-ask-pill {{
            display: none !important;
            pointer-events: none !important;
        }}

        .sb-subscribe-form {{
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin-top: 10px;
            align-items: center;
        }}
        .sb-subscribe-form .sb-email {{
            flex: 1;
            min-width: 200px;
        }}
        .sb-footer-signup {{
            margin-top: 10px;
            width: 100%;
            max-width: 520px;
        }}
        .sb-welcome-signup {{
            margin: 8px 0 4px;
            width: 100%;
        }}
        .sb-subscribe-status {{
            width: 100%;
            margin: 6px 0 0;
            font-size: 13px;
            font-weight: 600;
            color: var(--green);
            min-height: 1.2em;
        }}
        .sb-subscribe-status.is-error {{
            color: var(--red);
        }}
    </style>
</head>
<!-- NAV backdrop (right drawer) -->
<div class="sb-nav-backdrop" id="sb-nav-backdrop"></div>

<!-- NAV DRAWER (RIGHT) -->
<nav class="sb-nav-drawer" id="sb-nav-drawer">
  <div class="sb-nav-inner">
    <div class="sb-nav-header">
      <div class="sb-nav-header-left">
        <img src="assets/logo-transparent.png" alt="SnackBuddy logo" class="sb-logo" />
        <span class="sb-nav-title">SnackBuddy</span>
      </div>
      <button class="sb-nav-close" aria-label="Close menu">&times;</button>
    </div>

    <a href="#deals-list" class="sb-nav-link" data-view="deals">All Deals</a>
    <a href="?view=catalog" class="sb-nav-link" data-view="catalog" id="sb-nav-catalog">Browse all snacks</a>
    <a href="#about" class="sb-nav-link">About SnackBuddy</a>
    <a href="#how-it-works" class="sb-nav-link">How SnackBuddy Works</a>
    <a href="#how-we-pick-snacks" class="sb-nav-link">How We Pick Snacks</a>
    <a href="#subscribe" class="sb-nav-link">Don’t miss the ones worth a trip</a>

    <div class="sb-nav-footer">
      SnackBuddy helps you spot better-for-you snack deals.
    </div>
  </div>
</nav>


    <!-- ============================ -->
    <!-- SECTION 1A: NAVBAR           -->
    <!-- ============================ -->
    <header class="sb-header">
        <div class="sb-header-inner">
            <div class="sb-header-left">
                <img src="assets/logo-transparent.png" alt="SnackBuddy logo" class="sb-logo" />
                <span class="sb-header-title">SnackBuddy</span>
            </div>
            <div class="sb-header-freshness" title="Prices from Bowling Green, KY stores · checked {last_updated} Central · your store may differ">
                <span class="sb-header-freshness-label">Prices checked</span>
                <span class="sb-header-freshness-time">{last_updated_short}</span>
            </div>
            <button class="sb-menu-button" aria-label="Menu" aria-expanded="false">☰</button>
        </div>
    </header>

    <main class="page">
        <!-- ============================ -->
        <!-- SECTION 1B: HERO BANNER      -->
        <!-- ============================ -->
        <section class="sb-hero">
            <div class="sb-hero-bg">
                <div class="sb-hero-pattern"></div>
                <div class="sb-hero-content">
                    <div class="sb-hero-freshness" title="Prices from Bowling Green, KY stores · checked {last_updated} Central · your store may differ">
                        <span class="sb-hero-freshness-dot" aria-hidden="true"></span>
                        <span>Prices from Bowling Green, KY stores · checked {last_updated_short} · your store may differ</span>
                    </div>
                    <h1 class="sb-hero-title">Don&apos;t overpay for the snacks you already buy.</h1>
                    <p class="sb-hero-subtitle">{proof_line}</p>
                </div>
            </div>
        </section>

        {best_savings_html}

        <section class="sb-chip-row-section" id="sb-chip-row" aria-label="Filter deals">
            <div class="sb-chip-row-bar">
            <div class="sb-chip-row-scroll" id="sb-chip-row-scroll">
                <div class="sb-chip-row-track" id="sb-chip-row-track">
                    <div class="sb-chip-row-inner" id="sb-chip-row-inner">
                        <button type="button" class="sb-chip sb-chip-exit" id="sb-my-items-exit" hidden title="Back to all deals">★ My Items &times;</button>
                        <button type="button" class="sb-chip sb-chip-stores" id="sb-stores-chip" aria-haspopup="dialog" aria-expanded="false" aria-controls="sb-stores-popover" title="Choose your stores">
                            <span id="sb-stores-chip-label">My stores</span><span class="sb-chip-caret" aria-hidden="true">&#9662;</span>
                        </button>
                        <span class="sb-chip-divider" aria-hidden="true"></span>
                        <div class="sb-category-chips" id="sb-category-chips" role="group" aria-label="Category"></div>
                    </div>
                </div>
            </div>
            <div class="sb-chip-row-actions" id="sb-chip-row-actions">
                <button type="button" class="sb-chip sb-chip-icon" id="sb-filter-master" aria-label="Filters" aria-haspopup="dialog" aria-expanded="false" aria-controls="sb-filter-sheet">
                    <span aria-hidden="true">&#9881;&#65039;</span>
                    <span class="sb-chip-badge" id="sb-filter-master-count" hidden>0</span>
                </button>
                <button type="button" class="sb-chip sb-chip-icon" id="sb-search-toggle" aria-label="Search deals" aria-expanded="false" aria-controls="sb-search-wrap">
                    <span aria-hidden="true">&#128269;</span>
                </button>
            </div>
            </div>
            <div class="sb-search-wrap" id="sb-search-wrap" hidden>
                <label class="visually-hidden" for="sb-search-input">Search deals</label>
                <input
                    type="search"
                    id="sb-search-input"
                    class="sb-search-input"
                    placeholder="Search brand, product, or flavor…"
                    autocomplete="off"
                    enterkeyhint="search"
                />
                <button type="button" class="sb-search-clear" id="sb-search-clear" aria-label="Clear and close search">&times;</button>
            </div>
            <div class="sb-chip-meta">
                <button type="button" class="sb-legend-link" id="sb-legend-toggle" aria-expanded="false" aria-controls="sb-badge-legend">What do these badges mean?</button>
                <div class="sb-results-count" id="sb-results-count" aria-live="polite"></div>
            </div>
            <div class="sb-badge-legend" id="sb-badge-legend" hidden>
                <ul class="sb-badge-legend-list">
                    <li><span aria-hidden="true">💎</span> <strong>Diamond</strong> — 30%+ off its usual price</li>
                    <li><span aria-hidden="true">🔥</span> <strong>Fire</strong> — 20–29% off</li>
                    <li><span aria-hidden="true">💪</span> <strong>Strong</strong> — 10–19% off</li>
                    <li><span aria-hidden="true">🏷️</span> <strong>On Sale</strong> — 5–9% off</li>
                </ul>
            </div>
            <select id="sb-sort-select" class="sb-sort-select" aria-hidden="true" tabindex="-1">
                <option value="best" selected>Best discount (default)</option>
                <option value="pct-desc">% off: high to low</option>
                <option value="saved-desc">Most $ saved</option>
                <option value="price-asc">Price: low to high</option>
                <option value="price-desc">Price: high to low</option>
            </select>
        </section>
        <div class="sb-my-items-notify" id="sb-my-items-notify" hidden>
            <p class="sb-my-items-notify-title">Get notified when these go on sale</p>
            <p class="sb-my-items-notify-text">
                Want a heads-up when your starred items drop? Enter your email and we&apos;ll include them in SnackBuddy alerts.
            </p>
            <form class="sb-my-items-notify-form" id="sb-my-items-notify-form" novalidate>
                <div class="sb-my-items-notify-row">
                    <label class="visually-hidden" for="sb-my-items-notify-email">Email address</label>
                    <input
                        class="sb-my-items-notify-email"
                        id="sb-my-items-notify-email"
                        type="email"
                        name="email"
                        autocomplete="email"
                        inputmode="email"
                        enterkeyhint="done"
                        placeholder="Email address"
                    />
                    <button type="submit" class="sb-my-items-notify-submit" id="sb-my-items-notify-submit">
                        Notify me
                    </button>
                </div>
                <p class="sb-my-items-notify-status" id="sb-my-items-notify-status" aria-live="polite"></p>
            </form>
        </div>
        <div class="sb-auth-line" id="sb-auth-line" hidden></div>
        <div class="sb-filter-dropdown-backdrop" id="sb-filter-dropdown-backdrop"></div>
        <div class="sb-filter-dropdown-panel sb-stores-popover" id="sb-stores-popover" hidden role="dialog" aria-label="My stores"></div>
        <div class="sb-filter-sheet-backdrop" id="sb-filter-sheet-backdrop"></div>
        <aside class="sb-filter-sheet" id="sb-filter-sheet" aria-label="All filters">
            <div class="sb-filter-sheet-header">
                <div class="sb-filter-sheet-title">Filters</div>
                <button type="button" class="sb-filter-sheet-close" id="sb-filter-sheet-close" aria-label="Close filters">&times;</button>
            </div>
            <div class="sb-filter-sheet-body" id="sb-filter-sheet-body"></div>
            <div class="sb-filter-sheet-footer">
                <button type="button" class="sb-filter-sheet-clear" id="sb-filter-sheet-clear">Clear all</button>
                <button type="button" class="sb-filter-sheet-done" id="sb-filter-sheet-done">Done</button>
            </div>
        </aside>

        <div class="sb-deals-shell" id="sb-deals-shell">
        <div class="sb-deals-main">
        <p class="sb-stores-line" id="sb-stores-line" hidden>
            Your stores: <span id="sb-stores-line-names"></span>
            · <button type="button" id="sb-stores-line-change">Change</button>
        </p>
        <button type="button" class="sb-my-items-today-line" id="sb-my-items-today-line"></button>
        <section class="card-grid" id="deals-list">
            {cards_html}
        </section>
        <div class="sb-deals-empty" id="sb-deals-empty" role="status" aria-live="polite">
            No deals match your search or filters. Try clearing search or filters.
        </div>
        <div class="sb-my-items-empty" id="sb-my-items-empty" role="status" aria-live="polite">
            No saved items yet. Tap the ★ on any deal card to save it here — we’ll track when it goes on sale at each store.
        </div>
        <p class="sb-browse-catalog" id="sb-browse-catalog-wrap">
            <button type="button" class="sb-browse-catalog-link" id="sb-browse-catalog">Browse all snacks →</button>
        </p>

        </div>
        </div>

        <section class="sb-catalog" id="sb-catalog" hidden aria-label="Snack catalog">
            <div class="sb-catalog-inner">
                <div class="sb-catalog-topbar" id="sb-catalog-topbar">
                    <button type="button" class="sb-catalog-back" id="sb-catalog-back">← Back to deals</button>
                    <div class="sb-catalog-search-wrap">
                        <label class="visually-hidden" for="sb-catalog-search">Search products</label>
                        <input
                            type="search"
                            id="sb-catalog-search"
                            class="sb-catalog-search"
                            placeholder="Search {catalog_count_fmt} products — Quest, Celsius, protein bars…"
                            autocomplete="off"
                            enterkeyhint="search"
                        />
                    </div>
                </div>
                <h2 class="sb-catalog-title">Catalog</h2>
                <div class="sb-catalog-chips" id="sb-catalog-chips" role="group" aria-label="Catalog categories"></div>
                <div class="sb-catalog-stores-row">
                    <button type="button" class="sb-chip sb-chip-stores" id="sb-catalog-stores-chip" aria-haspopup="dialog" aria-expanded="false" title="Choose your stores">
                        <span id="sb-catalog-stores-label">My stores</span><span class="sb-chip-caret" aria-hidden="true">&#9662;</span>
                    </button>
                    <div class="sb-catalog-count" id="sb-catalog-count" aria-live="polite"></div>
                </div>
                <div class="sb-catalog-list" id="sb-catalog-list"></div>
                <button type="button" class="sb-catalog-more" id="sb-catalog-more" hidden>Show more</button>
                <div class="sb-catalog-empty" id="sb-catalog-empty" hidden role="status"></div>
            </div>
        </section>

        <!-- ============================ -->
        <!-- SECTION 3A: ABOUT            -->
        <!-- ============================ -->
        <section class="sb-info" id="about">
            <div class="sb-info-card">
                <h2 class="sb-info-title">About SnackBuddy</h2>
                <p class="sb-info-text">
                    SnackBuddy started after I became a dad, money got tighter, and I tried to get healthier at the same time.
                </p>
                <p class="sb-info-text" style="margin-top: 8px;">
                    When you’re trying to eat better, snacks matter. They can either quietly wreck a calorie deficit or help you hit goals like
                    <strong>protein</strong> and <strong>fiber</strong> without much effort. I found myself buying the same products over and over,
                    and I started noticing something: one week they’d be on sale, the next week they wouldn’t. After a while, paying full price
                    started to feel like I was overpaying for things I bought all the time.
                </p>
                <p class="sb-info-text" style="margin-top: 8px;">
                    So I built SnackBuddy to do one simple thing:
                    <strong>track the snacks and drinks people already buy and call out real price drops</strong>,
                    so you can stock up at the right time or try something new when it’s cheaper.
                </p>
            </div>
        </section>

        <!-- ============================ -->
        <!-- SECTION 3B: HOW IT WORKS     -->
        <!-- ============================ -->
        <section class="sb-info" id="how-it-works">
            <div class="sb-info-card">
                <h2 class="sb-info-title">How SnackBuddy Works</h2>
                <p class="sb-info-text">
                    SnackBuddy tracks prices for a growing list of better-for-you snacks and drinks across major retailers.
                </p>
                <p class="sb-info-text" style="margin-top: 8px;">
                    At its core, it’s pretty simple: we keep an eye on specific product pages and pay attention when prices move.
                    When something drops, it shows up here so more people can take advantage of it, without having to constantly
                    check themselves.
                </p>
                <p class="sb-info-text" style="margin-top: 8px;">
                    Don’t see a product or retailer you want tracked yet? Let me know, the list is expanding over time.
                </p>
                <p class="sb-note">
                    Note: Prices and availability can vary by location, especially for in-store items.
                </p>
            </div>
        </section>

        <!-- ============================ -->
        <!-- SECTION 3C: HOW WE PICK      -->
        <!-- ============================ -->
        <section class="sb-info" id="how-we-pick-snacks">
            <div class="sb-info-card">
                <h2 class="sb-info-title">How We Pick Snacks</h2>
                <p class="sb-info-text">
                    Not everything that calls itself healthy earns a spot here. Every product is manually reviewed, we&apos;re looking for real protein content relative to calories, low added sugar, and meaningful fiber where it applies.
                </p>
                <p class="sb-info-text" style="margin-top: 8px;">
                    We&apos;re not here to be perfect. SnackBuddy isn&apos;t about finding the most aggressive diet food on the shelf. It&apos;s about finding things that are genuinely better than the default, snacks people actually want to eat, at a price worth buying them.
                </p>
                <p class="sb-info-text" style="margin-top: 8px;">
                    Don&apos;t see something you think belongs here? Let us know, the list is always growing.
                </p>
            </div>
        </section>

            <div class="sb-info-card" id="subscribe">
                <h2 class="sb-info-title">Don’t miss the ones worth a trip</h2>
                <p class="sb-info-text">
                    We’ll send you the best savings every week, along with your favorite items when they go on sale.
                </p>

                <form class="sb-footer-signup" id="sb-footer-signup" novalidate>
                    <div class="sb-welcome-email-row">
                        <label class="visually-hidden" for="sb-footer-email">Email address</label>
                        <input
                            class="sb-welcome-email"
                            id="sb-footer-email"
                            type="email"
                            name="email"
                            autocomplete="email"
                            inputmode="email"
                            enterkeyhint="done"
                            placeholder="Email address"
                        />
                        <button type="submit" class="sb-welcome-email-submit" id="sb-footer-email-submit">
                            Subscribe
                        </button>
                    </div>
                    <p class="sb-subscribe-status" id="sb-footer-email-status" aria-live="polite"></p>
                </form>

                <a class="sb-subscribe-alt"
                   href="mailto:savewithsnackbuddy@gmail.com?subject=SnackBuddy%20Daily%20Deals">
                    Or email us: savewithsnackbuddy@gmail.com
                </a>

                <div class="sb-note">
                    No spam. Unsubscribe anytime.
                </div>
            </div>
        </section>
    </main>

    <div class="sb-welcome" id="sb-welcome" hidden aria-hidden="true">
      <div class="sb-welcome-backdrop" aria-hidden="true"></div>
      <div class="sb-welcome-panel" role="dialog" aria-modal="true" aria-labelledby="sb-welcome-brand">
        <button type="button" class="sb-welcome-close" id="sb-welcome-close" aria-label="Close">&times;</button>
        <div class="sb-welcome-panel-header">
          <div class="sb-welcome-progress" aria-hidden="true">
            <span class="sb-welcome-dot is-on" data-dot="0"></span>
            <span class="sb-welcome-dot" data-dot="1"></span>
            <span class="sb-welcome-dot" data-dot="2"></span>
            <span class="sb-welcome-dot" data-dot="3"></span>
          </div>
        </div>

        <section class="sb-welcome-step is-active" data-step="0">
          <div class="sb-welcome-panel-scroll">
            <div class="sb-welcome-kicker">Better snacks. Smarter buys.</div>
            <h1 class="sb-welcome-brand" id="sb-welcome-brand">Snack<span>Buddy</span></h1>
            <p class="sb-welcome-lead">Your better-for-you deals, cut down to the stores and snacks you actually buy.</p>
          </div>
          <div class="sb-welcome-panel-footer">
            <div class="sb-welcome-actions">
              <button type="button" class="sb-welcome-btn" data-welcome-next>Personalize my deals</button>
              <button type="button" class="sb-welcome-btn sb-welcome-btn-ghost" data-welcome-skip>Just show everything</button>
            </div>
          </div>
        </section>

        <section class="sb-welcome-step" data-step="1">
          <div class="sb-welcome-panel-scroll">
            <h2 class="sb-welcome-step-title">Where do you shop?</h2>
            <p class="sb-welcome-step-text">Pick any stores to narrow the list, or leave them all off to keep every store.</p>
            <div class="sb-welcome-chips" id="sb-welcome-retailers"></div>
            <div class="sb-welcome-other" id="sb-welcome-other-wrap" hidden>
              <label class="sb-welcome-other-label" for="sb-welcome-other-store">Which store?</label>
              <input
                class="sb-welcome-other-input"
                id="sb-welcome-other-store"
                type="text"
                maxlength="80"
                placeholder="e.g. Costco, Aldi, Publix…"
                autocomplete="organization"
              />
              <p class="sb-welcome-other-hint">We’ll use this to decide which stores to add next.</p>
            </div>
            <p class="sb-welcome-preview" id="sb-welcome-preview-stores"></p>
            <p class="sb-welcome-edit-hint" id="sb-welcome-edit-hint" hidden>
              Sign in to save this to your email alerts too.
            </p>
          </div>
          <div class="sb-welcome-panel-footer">
            <div class="sb-welcome-actions">
              <button type="button" class="sb-welcome-btn" data-welcome-next data-welcome-stores-next>Next</button>
              <button type="button" class="sb-welcome-btn sb-welcome-btn-ghost" data-welcome-back>Back</button>
              <button type="button" class="sb-welcome-btn sb-welcome-btn-ghost" data-welcome-edit-cancel hidden>Cancel</button>
            </div>
          </div>
        </section>

        <section class="sb-welcome-step" data-step="2">
          <div class="sb-welcome-panel-scroll">
            <h2 class="sb-welcome-step-title">What’s usually in your pantry?</h2>
            <p class="sb-welcome-step-text">Pick the categories you restock most, or leave them off to keep everything.</p>
            <div class="sb-welcome-chips" id="sb-welcome-categories"></div>
            <p class="sb-welcome-preview" id="sb-welcome-preview-categories"></p>
          </div>
          <div class="sb-welcome-panel-footer">
            <div class="sb-welcome-actions">
              <button type="button" class="sb-welcome-btn" data-welcome-next>See my deals</button>
              <button type="button" class="sb-welcome-btn sb-welcome-btn-ghost" data-welcome-back>Back</button>
            </div>
          </div>
        </section>

        <section class="sb-welcome-step" data-step="3">
          <div class="sb-welcome-panel-scroll">
            <h2 class="sb-welcome-step-title">Don’t miss the ones worth a trip</h2>
            <p class="sb-welcome-step-text">Optional. We’ll email when the good stuff drops, your filters stay either way.</p>
            <form class="sb-welcome-signup" id="sb-welcome-signup" novalidate>
              <div class="sb-welcome-email-row">
                <input
                  class="sb-welcome-email"
                  id="sb-welcome-email"
                  type="email"
                  name="email"
                  autocomplete="email"
                  inputmode="email"
                  enterkeyhint="done"
                  placeholder="Email address"
                  aria-label="Email address"
                />
                <button type="submit" class="sb-welcome-email-submit" id="sb-welcome-email-submit">
                  Subscribe
                </button>
              </div>
              <p class="sb-welcome-status" id="sb-welcome-email-status" aria-live="polite"></p>
            </form>
            <p class="sb-welcome-note">No spam. Unsubscribe anytime. Prefs stay on this device.</p>
          </div>
          <div class="sb-welcome-panel-footer">
            <div class="sb-welcome-actions">
              <button type="button" class="sb-welcome-btn" data-welcome-next>See my deals</button>
              <button type="button" class="sb-welcome-btn sb-welcome-btn-ghost" data-welcome-skip>Skip for now</button>
            </div>
          </div>
        </section>
      </div>
    </div>

    <div class="sb-welcome" id="sb-prefs" hidden aria-hidden="true">
      <div class="sb-welcome-backdrop" data-prefs-cancel aria-hidden="true"></div>
      <div class="sb-welcome-panel" role="dialog" aria-modal="true" aria-labelledby="sb-prefs-title">
        <button type="button" class="sb-welcome-close" id="sb-prefs-close" data-prefs-cancel aria-label="Close">&times;</button>
        <div class="sb-welcome-panel-header">
          <div class="sb-welcome-kicker">Your setup</div>
          <h1 class="sb-welcome-brand" id="sb-prefs-title">Preferences</h1>
          <p class="sb-welcome-lead">Stores, snacks, and whether you want The Weekly Snack.</p>
        </div>

        <div class="sb-welcome-panel-scroll">
          <section class="sb-prefs-section">
            <h2 class="sb-prefs-section-title">Your stores</h2>
            <p class="sb-prefs-section-text">Pick any stores to narrow the list, or leave them all off to keep every store.</p>
            <div class="sb-welcome-chips" id="sb-prefs-retailers"></div>
            <div class="sb-welcome-other" id="sb-prefs-other-wrap" hidden>
              <label class="sb-welcome-other-label" for="sb-prefs-other-store">Which store?</label>
              <input
                class="sb-welcome-other-input"
                id="sb-prefs-other-store"
                type="text"
                maxlength="80"
                placeholder="e.g. Costco, Aldi, Publix…"
                autocomplete="organization"
              />
              <p class="sb-welcome-other-hint">We’ll use this to decide which stores to add next.</p>
            </div>
          </section>

          <section class="sb-prefs-section">
            <h2 class="sb-prefs-section-title">What you snack on</h2>
            <p class="sb-prefs-section-text">Pick the categories you restock most, or leave them off to keep everything.</p>
            <div class="sb-welcome-chips" id="sb-prefs-categories"></div>
          </section>

          <section class="sb-prefs-section" id="sb-prefs-email-section">
            <h2 class="sb-prefs-section-title">Emails</h2>
            <div id="sb-prefs-email-signed-in" hidden>
              <label class="sb-prefs-toggle" for="sb-prefs-weekly">
                <input type="checkbox" id="sb-prefs-weekly" />
                <span>Send me The Weekly Snack</span>
              </label>
            </div>
            <div id="sb-prefs-email-signed-out" hidden>
              <p class="sb-prefs-signin-note">Sign in to get The Weekly Snack and save these across devices.</p>
              <form class="sb-welcome-signup" id="sb-prefs-signup" novalidate>
                <div class="sb-welcome-email-row">
                  <input
                    class="sb-welcome-email"
                    id="sb-prefs-email"
                    type="email"
                    name="email"
                    autocomplete="email"
                    inputmode="email"
                    enterkeyhint="done"
                    placeholder="Email address"
                    aria-label="Email address"
                  />
                  <button type="submit" class="sb-welcome-email-submit" id="sb-prefs-email-submit">
                    Sign in
                  </button>
                </div>
                <p class="sb-welcome-status" id="sb-prefs-email-status" aria-live="polite"></p>
              </form>
            </div>
          </section>
        </div>

        <div class="sb-welcome-panel-footer">
          <div class="sb-prefs-actions">
            <button type="button" class="sb-welcome-btn" id="sb-prefs-save">Save preferences</button>
            <button type="button" class="sb-prefs-cancel" id="sb-prefs-cancel" data-prefs-cancel>Cancel</button>
          </div>
        </div>
      </div>
    </div>

    <footer class="sb-site-footer">
        <p>
            SnackBuddy uses Google Analytics to understand how people use the site
            (for example page views and which links are clicked). We don&apos;t sell
            your personal information. Email signups are only used to send deal digests
            you request.
        </p>
    </footer>

    <div class="sb-email-ask" id="sb-email-ask" hidden aria-live="polite">
      <button type="button" class="sb-email-ask-close" id="sb-email-ask-close" aria-label="Dismiss">&times;</button>
      <p class="sb-email-ask-title">⭐ Saved! Want an email when your starred items go on sale?</p>
      <form class="sb-email-ask-form" id="sb-email-ask-form" novalidate>
        <div class="sb-email-ask-row">
          <label class="visually-hidden" for="sb-email-ask-email">Email address</label>
          <input
            class="sb-email-ask-email"
            id="sb-email-ask-email"
            type="email"
            name="email"
            autocomplete="email"
            inputmode="email"
            enterkeyhint="done"
            placeholder="Email address"
          />
          <button type="submit" class="sb-email-ask-submit" id="sb-email-ask-submit">Notify me</button>
        </div>
        <p class="sb-email-ask-note">Only when it&apos;s worth it. Unsubscribe anytime.</p>
        <p class="sb-email-ask-status" id="sb-email-ask-status"></p>
      </form>
    </div>
    <div class="sb-savings-panel" id="sb-savings-panel" hidden role="dialog" aria-label="Your favorites savings" aria-live="polite">
      <button type="button" class="sb-savings-panel-close" id="sb-savings-panel-close" aria-label="Close">&times;</button>
      <p class="sb-savings-panel-headline" id="sb-savings-panel-headline"></p>
      <p class="sb-savings-panel-line" id="sb-savings-panel-onsale"></p>
      <p class="sb-savings-panel-line" id="sb-savings-panel-eight"></p>
      <div class="sb-savings-panel-qty" id="sb-savings-panel-qty">
        <span>Buy</span>
        <button type="button" class="sb-savings-qty-btn is-active" data-qty="1" aria-pressed="true">1</button>
        <button type="button" class="sb-savings-qty-btn" data-qty="2" aria-pressed="false">2</button>
        <button type="button" class="sb-savings-qty-btn" data-qty="3" aria-pressed="false">3</button>
        <span>each sale</span>
      </div>
      <div class="sb-savings-panel-how" id="sb-savings-panel-how">
        <p class="sb-savings-panel-how-title">How we figured this</p>
        <ul class="sb-savings-panel-list" id="sb-savings-panel-list"></ul>
      </div>
    </div>
    <button type="button" class="sb-email-ask-pill" id="sb-email-ask-pill" data-mode="alerts" hidden>⭐ Favorites Alerts</button>
    <div class="sb-star-toast" id="sb-star-toast" hidden role="status" aria-live="polite">Saved ⭐ We&apos;ll let you know when it&apos;s on sale</div>

<script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2"></script>
<script>
document.addEventListener("DOMContentLoaded", function () {{
  const grid = document.getElementById("deals-list");
  const cards = Array.from(document.querySelectorAll(".card"));
  const searchInput = document.getElementById("sb-search-input");
  const searchWrap = document.getElementById("sb-search-wrap");
  const searchClear = document.getElementById("sb-search-clear");
  const sortSelect = document.getElementById("sb-sort-select");
  const dealsEmpty = document.getElementById("sb-deals-empty");

  function trackEvent(name, params) {{
    try {{
      if (typeof gtag === "function") {{
        gtag("event", name, params || {{}});
      }}
    }} catch (err) {{}}
  }}

  if (grid) {{
    grid.addEventListener("click", function(ev) {{
      const link = ev.target && ev.target.closest
        ? ev.target.closest("a.view-button, a.card-carousel-link")
        : null;
      if (!link || !grid.contains(link)) return;
      const card = link.closest(".card");
      if (!card) return;
      const titleEl = card.querySelector(".card-title");
      const itemName = titleEl ? String(titleEl.textContent || "").trim() : "";
      trackEvent("retailer_click", {{
        retailer: card.dataset.retailer || "",
        item_name: itemName,
        brand: card.dataset.brand || "",
        deal_tier: card.dataset.tier || "",
        percent_off: parseFloat(card.dataset.percent || "0") || 0,
        price: parseFloat(card.dataset.price || "0") || 0,
        link_type: link.classList.contains("view-button") ? "view_button" : "image"
      }});
    }});
  }}

  const filterSheetBody = document.getElementById("sb-filter-sheet-body");
  const filterMasterBtn = document.getElementById("sb-filter-master");
  const filterMasterCount = document.getElementById("sb-filter-master-count");
  const resultsCount = document.getElementById("sb-results-count");
  const filterDropdownBackdrop = document.getElementById("sb-filter-dropdown-backdrop");
  const filterSheet = document.getElementById("sb-filter-sheet");
  const filterSheetBackdrop = document.getElementById("sb-filter-sheet-backdrop");
  const filterSheetClose = document.getElementById("sb-filter-sheet-close");
  const filterSheetClear = document.getElementById("sb-filter-sheet-clear");
  const filterSheetDone = document.getElementById("sb-filter-sheet-done");
  const storesChip = document.getElementById("sb-stores-chip");
  const storesChipLabel = document.getElementById("sb-stores-chip-label");
  const storesPopover = document.getElementById("sb-stores-popover");
  const categoryChipsBox = document.getElementById("sb-category-chips");
  const myItemsExitChip = document.getElementById("sb-my-items-exit");
  const searchToggle = document.getElementById("sb-search-toggle");
  const chipRowScroll = document.getElementById("sb-chip-row-scroll");
  const chipRowTrack = document.getElementById("sb-chip-row-track");
  const legendToggle = document.getElementById("sb-legend-toggle");
  const badgeLegend = document.getElementById("sb-badge-legend");

  /* ============================
     NAV DRAWER (RIGHT)
     ============================ */
  const navBtn = document.querySelector(".sb-menu-button");
  const navDrawer = document.getElementById("sb-nav-drawer");
  const navBackdrop = document.getElementById("sb-nav-backdrop");
  const navClose = document.querySelector(".sb-nav-close");

  function openNav() {{
    if (!navDrawer || !navBackdrop || !navBtn) return;
    navDrawer.classList.add("open");
    navBackdrop.classList.add("open");
    navBtn.setAttribute("aria-expanded", "true");
  }}

  function closeNav() {{
    if (!navDrawer || !navBackdrop || !navBtn) return;
    navDrawer.classList.remove("open");
    navBackdrop.classList.remove("open");
    navBtn.setAttribute("aria-expanded", "false");
  }}

  navBtn?.addEventListener("click", function() {{
    if (navDrawer?.classList.contains("open")) closeNav();
    else openNav();
  }});
  navBackdrop?.addEventListener("click", closeNav);
  navClose?.addEventListener("click", closeNav);

  /* close nav when clicking nav links */
  if (navDrawer) {{
    const navLinks = navDrawer.querySelectorAll("a.sb-nav-link");
    navLinks.forEach(function(link) {{
      link.addEventListener("click", closeNav);
    }});
  }}

  /* ============================
     FILTER BAR (Walmart-style pills)
     ============================ */
  if (!grid || !cards.length || !chipRowTrack) {{
    return;
  }}

  cards.forEach(function(card, idx) {{
    card.dataset.originalIndex = String(idx);
  }});

  function uniq(values) {{
    return Array.from(new Set(values.filter(Boolean))).sort();
  }}

  function titleize(s) {{
    if (!s) return "";
    const specialCases = {{
      "rtd": "RTD",
      "protein": "Protein",
      "drink": "Drink",
      "shake": "Shake",
      "snack": "Snack",
      "crunchy": "Crunchy",
      "sweets": "Sweets",
      "chips": "Chips",
      "cookies": "Cookies",
      "meat": "Meat",
      "energy": "Energy"
    }};
    const parts = s.split(/([\\s&\\-])/);
    return parts.map(function(part) {{
      if (part.match(/^[\\s&\\-]$/)) return part;
      const lower = part.toLowerCase();
      if (specialCases[lower]) return specialCases[lower];
      return part.charAt(0).toUpperCase() + part.slice(1).toLowerCase();
    }}).join("");
  }}

  const FILTER_DEFS = [
    {{ key: "retailer", label: "Retailer" }},
    {{ key: "section", label: "Section" }},
    {{ key: "category", label: "Category" }},
    {{ key: "brand", label: "Brand" }},
  ];
  // Filters sheet only builds Section + Brand lists (Retailer = My stores chip, Category = chip row).
  const SHEET_DEFS = FILTER_DEFS.filter(function(def) {{
    return def.key === "section" || def.key === "brand";
  }});
  const TIER_OPTIONS = [
    {{ value: "diamond", label: "💎 Diamond" }},
    {{ value: "fire", label: "🔥 Fire" }},
    {{ value: "strong", label: "💪 Strong" }},
    {{ value: "sale", label: "🏷️ On Sale" }},
  ];
  const SORT_OPTIONS = [
    {{ value: "best", label: "Best discount (default)" }},
    {{ value: "pct-desc", label: "% off: high to low" }},
    {{ value: "saved-desc", label: "Most $ saved" }},
    {{ value: "price-asc", label: "Price: low to high" }},
    {{ value: "price-desc", label: "Price: high to low" }},
  ];
  const CATEGORY_PREFERRED = [
    "bars",
    "energy drink",
    "rtd protein shake",
    "protein powder",
    "chips & crunchy",
  ];
  const CATEGORY_CHIP_LABELS = {{
    "bars": "Bars",
    "energy drink": "Drinks",
    "rtd protein shake": "Shakes",
    "protein powder": "Powder",
    "chips & crunchy": "Chips & Snacks",
    "cookies & sweets": "Cookies & Sweets",
    "meat snack": "Meat Stick",
    "wraps": "Wraps",
  }};

  const selected = {{
    retailer: new Set(),
    section: new Set(),
    category: new Set(),
    brand: new Set(),
  }};
  let selectedTiers = new Set();
  let myItemsView = false;
  const MY_ITEMS_KEY = "sb_my_items_v1";
  const MY_ITEMS_NAMES_KEY = "sb_my_items_names_v1";
  const STAR_PENDING_KEY = "sb_star_pending_v1";
  const EMAIL_SUB_KEY = "sb_email_subscribed_v1";
  const EMAIL_ASKS_KEY = "sb_email_asks_v1";
  const SUPABASE_URL = "{supabase_url}";
  const SUPABASE_ANON_KEY = "{supabase_anon_key}";
  const MAGIC_LINK_MSG = "Check your email: tap the link to confirm and save your items.";
  const myItemsTodayLine = document.getElementById("sb-my-items-today-line");
  const savingsPanel = document.getElementById("sb-savings-panel");
  const savingsPanelClose = document.getElementById("sb-savings-panel-close");
  const savingsPanelHeadline = document.getElementById("sb-savings-panel-headline");
  const savingsPanelOnSale = document.getElementById("sb-savings-panel-onsale");
  const savingsPanelEight = document.getElementById("sb-savings-panel-eight");
  const savingsPanelQty = document.getElementById("sb-savings-panel-qty");
  const savingsPanelHow = document.getElementById("sb-savings-panel-how");
  const savingsPanelList = document.getElementById("sb-savings-panel-list");
  let savingsBuyQty = 1;
  const myItemsNotify = document.getElementById("sb-my-items-notify");
  const myItemsEmpty = document.getElementById("sb-my-items-empty");
  const authLine = document.getElementById("sb-auth-line");
  const emailAsk = document.getElementById("sb-email-ask");
  const emailAskForm = document.getElementById("sb-email-ask-form");
  const emailAskEmail = document.getElementById("sb-email-ask-email");
  const emailAskSubmit = document.getElementById("sb-email-ask-submit");
  const emailAskStatus = document.getElementById("sb-email-ask-status");
  const emailAskClose = document.getElementById("sb-email-ask-close");
  const emailAskPill = document.getElementById("sb-email-ask-pill");
  const starToast = document.getElementById("sb-star-toast");
  const offSaleCards = new Map();
  let lastSaleItems = {{}};
  let savingsByItem = {{}};
  // "pending" | "ok" | "error" — never treat a failed fetch as "too new".
  let savingsLoadStatus = "pending";
  let sbAuthUser = null;
  let sbSyncing = false;
  let emailAskShownThisVisit = false;
  let emailAskPillActive = false;
  let welcomeEmailAskCounted = false;
  let emailAskTimer = null;
  let emailAskRemainingMs = 0;
  let emailAskDeadline = 0;
  let starToastTimer = null;

  const sbClient = (window.supabase && typeof window.supabase.createClient === "function")
    ? window.supabase.createClient(SUPABASE_URL, SUPABASE_ANON_KEY)
    : null;
  if (!sbClient) {{
    console.error("[SnackBuddy] supabase-js failed to load; email sync disabled.");
  }}

  function hasEmailSubscription() {{
    try {{
      return localStorage.getItem(EMAIL_SUB_KEY) === "1";
    }} catch (err) {{
      return false;
    }}
  }}
  function markEmailSubscribed() {{
    try {{
      localStorage.setItem(EMAIL_SUB_KEY, "1");
    }} catch (err) {{}}
    syncFloatButton();
  }}
  function getEmailAskCount() {{
    try {{
      const n = parseInt(localStorage.getItem(EMAIL_ASKS_KEY) || "0", 10);
      return (isFinite(n) && n > 0) ? n : 0;
    }} catch (err) {{
      return 0;
    }}
  }}
  function recordEmailAsk() {{
    const next = getEmailAskCount() + 1;
    try {{
      localStorage.setItem(EMAIL_ASKS_KEY, String(next));
    }} catch (err) {{}}
    return next;
  }}
  function canShowEmailAsk() {{
    // Full banner at most once/visit; collapsing to pill does not consume an ask count.
    return !sbAuthUser && !hasEmailSubscription() && getEmailAskCount() < 2 && !emailAskShownThisVisit && !emailAskPillActive;
  }}
  function setEmailAskStatus(msg, isError) {{
    if (!emailAskStatus) return;
    emailAskStatus.textContent = msg || "";
    emailAskStatus.classList.toggle("is-error", !!isError);
  }}
  function clearEmailAskTimer() {{
    if (emailAskTimer) {{
      window.clearTimeout(emailAskTimer);
      emailAskTimer = null;
    }}
  }}
  function hideEmailAskPill() {{
    emailAskPillActive = false;
    syncFloatButton();
  }}
  function showEmailAskPill() {{
    if (!emailAskPill || sbAuthUser || hasEmailSubscription()) return;
    emailAskPillActive = true;
    syncFloatButton();
  }}

  /* ============================
     ONE CONTEXT-AWARE FLOAT BUTTON
     modes: alerts (Favorites Alerts) | items (My Items (N)) | savings (💲 $X)
     ============================ */
  function setFloatButton(mode, label) {{
    if (!emailAskPill) return;
    if (!mode) {{
      emailAskPill.classList.remove("is-on");
      window.setTimeout(function() {{
        if (emailAskPill && !emailAskPill.classList.contains("is-on")) emailAskPill.hidden = true;
      }}, 280);
      return;
    }}
    emailAskPill.dataset.mode = mode;
    emailAskPill.textContent = label;
    emailAskPill.setAttribute("aria-label",
      mode === "savings" ? "See how much your favorites could save" :
      (mode === "items" ? "Open My Items" : "Favorites alerts"));
    const wasHidden = emailAskPill.hidden;
    emailAskPill.hidden = false;
    if (wasHidden) void emailAskPill.offsetWidth;
    emailAskPill.classList.add("is-on");
  }}
  function isWelcomeOrPrefsOpen() {{
    const welcomeEl = document.getElementById("sb-welcome");
    const prefsEl = document.getElementById("sb-prefs");
    return !!(
      (welcomeEl && welcomeEl.classList.contains("open") && !welcomeEl.hidden) ||
      (prefsEl && prefsEl.classList.contains("open") && !prefsEl.hidden)
    );
  }}
  function syncFloatButton() {{
    if (!emailAskPill) return;
    const bannerOpen = !!(emailAsk && emailAsk.classList.contains("is-on"));
    const panelOpen = !!(savingsPanel && savingsPanel.classList.contains("is-on"));
    const modalOpen = isWelcomeOrPrefsOpen();
    const nStars = starredIds.size;
    const signedUp = !!sbAuthUser || hasEmailSubscription();

    // Panel only makes sense on My Items with stars.
    if (panelOpen && !(myItemsView && nStars > 0)) {{
      closeSavingsPanel();
      return;
    }}
    if (bannerOpen || panelOpen || modalOpen) {{
      setFloatButton("", "");
      return;
    }}

    if (myItemsView) {{
      if (nStars > 0) {{
        const today = computeTodaySavings().total;
        const whole = Math.round(today);
        setFloatButton("savings", whole > 0 ? ("💲 $" + whole) : "💲");
      }} else {{
        setFloatButton("", "");
      }}
      return;
    }}
    if (!signedUp) {{
      if (emailAskPillActive) setFloatButton("alerts", "⭐ Favorites Alerts");
      else setFloatButton("", "");
      return;
    }}
    if (nStars > 0) setFloatButton("items", "⭐ My Items (" + nStars + ")");
    else setFloatButton("", "");
  }}
  function hideEmailAskBannerOnly() {{
    clearEmailAskTimer();
    emailAskRemainingMs = 0;
    emailAskDeadline = 0;
    document.body.classList.remove("sb-email-ask-open");
    if (!emailAsk) return;
    emailAsk.classList.remove("is-on");
    window.setTimeout(function() {{
      if (emailAsk && !emailAsk.classList.contains("is-on")) emailAsk.hidden = true;
    }}, 300);
    syncFloatButton();
  }}
  function dismissEmailAsk(countAsAsk) {{
    // Permanent dismiss for this interaction (× / submit / sign-in).
    hideEmailAskBannerOnly();
    hideEmailAskPill();
    if (countAsAsk) recordEmailAsk();
  }}
  function collapseEmailAskToPill() {{
    // Timeout with no interaction: keep the invite as a pill; do NOT count an ask.
    hideEmailAskBannerOnly();
    showEmailAskPill();
  }}
  function scheduleEmailAskAutoCollapse(ms) {{
    clearEmailAskTimer();
    const wait = (typeof ms === "number" && ms > 0) ? ms : 15000;
    emailAskRemainingMs = wait;
    emailAskDeadline = Date.now() + wait;
    emailAskTimer = window.setTimeout(function() {{
      emailAskTimer = null;
      if (emailAskEmail && document.activeElement === emailAskEmail) {{
        // Still focused — check again after a short delay.
        scheduleEmailAskAutoCollapse(1500);
        return;
      }}
      collapseEmailAskToPill();
    }}, wait);
  }}
  function pauseEmailAskAutoCollapse() {{
    if (!emailAskTimer) return;
    emailAskRemainingMs = Math.max(0, emailAskDeadline - Date.now());
    clearEmailAskTimer();
  }}
  function resumeEmailAskAutoCollapse() {{
    if (!emailAsk || !emailAsk.classList.contains("is-on")) return;
    if (emailAskRemainingMs <= 0) {{
      collapseEmailAskToPill();
      return;
    }}
    scheduleEmailAskAutoCollapse(emailAskRemainingMs);
  }}
  function openEmailAskBanner(opts) {{
    opts = opts || {{}};
    const fromPill = !!opts.fromPill;
    if (!emailAsk) return false;
    if (!fromPill && !canShowEmailAsk()) return false;
    if (sbAuthUser || hasEmailSubscription()) return false;

    emailAskShownThisVisit = true;
    if (savingsPanel && savingsPanel.classList.contains("is-on")) closeSavingsPanel();
    hideEmailAskPill();
    emailAsk.hidden = false;
    setEmailAskStatus("");
    if (emailAskSubmit) emailAskSubmit.disabled = false;
    if (!fromPill && emailAskEmail) emailAskEmail.value = "";
    void emailAsk.offsetWidth;
    emailAsk.classList.add("is-on");
    document.body.classList.add("sb-email-ask-open");
    syncFloatButton();
    scheduleEmailAskAutoCollapse(15000);
    return true;
  }}
  function showEmailAskBanner() {{
    return openEmailAskBanner({{ fromPill: false }});
  }}
  function maybeAskEmailAfterStar() {{
    if (emailAskPillActive) return; // pill already represents this visit's ask
    if (!canShowEmailAsk()) return;
    if (starredIds.size >= 2) showEmailAskBanner();
  }}
  function maybeAskEmailOnMyItemsOpen() {{
    if (emailAskPillActive) return;
    if (!canShowEmailAsk()) return;
    if (starredIds.size >= 1) showEmailAskBanner();
  }}
  function showAppToast(message) {{
    if (!starToast) return;
    starToast.textContent = message || "";
    starToast.hidden = false;
    void starToast.offsetWidth;
    starToast.classList.add("is-on");
    if (starToastTimer) window.clearTimeout(starToastTimer);
    starToastTimer = window.setTimeout(function() {{
      starToast.classList.remove("is-on");
      window.setTimeout(function() {{
        if (starToast && !starToast.classList.contains("is-on")) starToast.hidden = true;
      }}, 280);
    }}, 3200);
  }}
  function showStarSavedToast() {{
    showAppToast("Saved ⭐ We’ll let you know when it’s on sale");
  }}
  function showStoresSavedToast(stores) {{
    const list = Array.isArray(stores)
      ? stores.map(function(v) {{ return String(v || "").trim(); }}).filter(Boolean)
      : [];
    let msg = list.length
      ? ("Saved. Showing deals from " + list.map(displayRetailerName).join(", ") + ".")
      : "Saved. Showing deals from all stores.";
    if (sbAuthUser) msg += " Your emails will use these too.";
    showAppToast(msg);
  }}
  function syncMyItemsNotifyVisibility() {{
    if (!myItemsNotify) return;
    // Always available at the top of My Items when not signed in.
    const show = myItemsView && !sbAuthUser;
    myItemsNotify.hidden = !show;
  }}
  function wireEmailAskBanner() {{
    if (emailAskClose) {{
      emailAskClose.addEventListener("click", function() {{
        dismissEmailAsk(true); // × counts toward the 2-ask limit
      }});
    }}
    if (emailAskPill) {{
      emailAskPill.addEventListener("click", function() {{
        const mode = emailAskPill.dataset.mode || "alerts";
        if (mode === "savings") {{
          openSavingsPanel();
        }} else if (mode === "items") {{
          if (!myItemsView) setTierSelection("my-items");
        }} else {{
          openEmailAskBanner({{ fromPill: true }});
        }}
      }});
    }}
    if (emailAskEmail) {{
      emailAskEmail.addEventListener("focus", pauseEmailAskAutoCollapse);
      emailAskEmail.addEventListener("blur", resumeEmailAskAutoCollapse);
    }}
    if (!emailAskForm || emailAskForm.dataset.wired === "1") return;
    emailAskForm.dataset.wired = "1";
    emailAskForm.addEventListener("submit", function(ev) {{
      ev.preventDefault();
      if (sbAuthUser) {{
        if (emailAskSubmit) emailAskSubmit.disabled = true;
        setEmailAskStatus("Re-enabling emails…");
        resubscribeSignedInUser().then(function(ok) {{
          setEmailAskStatus(ok
            ? "You’re back on the list — emails re-enabled."
            : "Couldn’t update preferences. Try again.");
          if (ok) window.setTimeout(function() {{ dismissEmailAsk(false); }}, 1600);
          else if (emailAskSubmit) emailAskSubmit.disabled = false;
        }});
        return;
      }}
      const email = emailAskEmail ? String(emailAskEmail.value || "").trim() : "";
      if (!isValidEmail(email)) {{
        setEmailAskStatus("Enter a valid email address.", true);
        if (emailAskEmail) emailAskEmail.focus();
        return;
      }}
      if (!sbClient) {{
        setEmailAskStatus("Signup is temporarily unavailable. Try again later.", true);
        return;
      }}
      clearEmailAskTimer();
      if (emailAskSubmit) emailAskSubmit.disabled = true;
      setEmailAskStatus("Sending link…");
      submitMagicLinkSignup(email).then(function() {{
        markEmailSubscribed();
        syncMyItemsNotifyVisibility();
        trackEvent("generate_lead", {{ method: "after_star_banner" }});
        setEmailAskStatus(MAGIC_LINK_MSG);
        if (emailAskEmail) emailAskEmail.blur();
        // Subscribed — dismiss banner + pill; submit does not count as an ask.
        window.setTimeout(function() {{ dismissEmailAsk(false); }}, 1600);
      }}).catch(function(err) {{
        console.error("[SnackBuddy] after-star magic link failed", err);
        setEmailAskStatus("Couldn’t send the link. Try again.", true);
        if (emailAskSubmit) emailAskSubmit.disabled = false;
        resumeEmailAskAutoCollapse();
      }});
    }});
  }}

  function loadStarredIds() {{
    try {{
      const raw = localStorage.getItem(MY_ITEMS_KEY);
      const arr = raw ? JSON.parse(raw) : [];
      return new Set(Array.isArray(arr) ? arr.filter(Boolean).map(String) : []);
    }} catch (err) {{
      return new Set();
    }}
  }}
  let starredIds = loadStarredIds();

  function saveStarredIds() {{
    try {{
      localStorage.setItem(MY_ITEMS_KEY, JSON.stringify(Array.from(starredIds)));
    }} catch (err) {{}}
  }}

  function loadStarNames() {{
    try {{
      const raw = localStorage.getItem(MY_ITEMS_NAMES_KEY);
      const obj = raw ? JSON.parse(raw) : {{}};
      return (obj && typeof obj === "object") ? obj : {{}};
    }} catch (err) {{
      return {{}};
    }}
  }}
  let starNames = loadStarNames();

  function saveStarNames() {{
    try {{
      localStorage.setItem(MY_ITEMS_NAMES_KEY, JSON.stringify(starNames));
    }} catch (err) {{}}
  }}

  function setStarName(itemId, itemName) {{
    if (!itemId) return;
    const name = String(itemName || "").trim();
    if (name) starNames[itemId] = name;
    else if (!starNames[itemId]) {{
      const meta = lastSaleItems[itemId];
      if (meta && meta.product_name) starNames[itemId] = String(meta.product_name);
    }}
    saveStarNames();
  }}

  function getStarName(itemId) {{
    if (starNames[itemId]) return starNames[itemId];
    const meta = lastSaleItems[itemId];
    if (meta && meta.product_name) return String(meta.product_name);
    return "";
  }}

  function loadPendingStars() {{
    try {{
      const raw = localStorage.getItem(STAR_PENDING_KEY);
      const arr = raw ? JSON.parse(raw) : [];
      return Array.isArray(arr) ? arr : [];
    }} catch (err) {{
      return [];
    }}
  }}

  function savePendingStars(arr) {{
    try {{
      localStorage.setItem(STAR_PENDING_KEY, JSON.stringify(arr || []));
    }} catch (err) {{}}
  }}

  function enqueueStarOp(op, itemId, itemName) {{
    if (!itemId) return;
    const q = loadPendingStars().filter(function(row) {{
      return !(row && row.item_id === itemId);
    }});
    q.push({{ op: op, item_id: itemId, item_name: itemName || getStarName(itemId) || "" }});
    savePendingStars(q);
  }}

  function magicLinkRedirectTo() {{
    try {{
      return window.location.origin + window.location.pathname + window.location.search;
    }} catch (err) {{
      return window.location.href;
    }}
  }}

  function requestMagicLink(email) {{
    if (!sbClient) {{
      return Promise.reject(new Error("supabase unavailable"));
    }}
    return sbClient.auth.signInWithOtp({{
      email: email,
      options: {{ emailRedirectTo: magicLinkRedirectTo() }},
    }}).then(function(res) {{
      if (res && res.error) throw res.error;
      return res;
    }});
  }}

  function updateAuthLine() {{
    if (!authLine) return;
    if (!sbAuthUser || !myItemsView) {{
      authLine.hidden = true;
      authLine.textContent = "";
      return;
    }}
    authLine.hidden = false;
    authLine.textContent = "";
    authLine.appendChild(document.createTextNode("Signed in as " + (sbAuthUser.email || "") + " · "));
    const prefsBtn = document.createElement("button");
    prefsBtn.type = "button";
    prefsBtn.textContent = "Preferences";
    prefsBtn.addEventListener("click", function() {{
      openStoresPicker();
    }});
    authLine.appendChild(prefsBtn);
    authLine.appendChild(document.createTextNode(" · "));
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "Sign out";
    btn.addEventListener("click", function() {{
      if (!sbClient) return;
      sbClient.auth.signOut().then(function(res) {{
        if (res && res.error) console.error("[SnackBuddy] signOut failed", res.error);
      }}).catch(function(err) {{
        console.error("[SnackBuddy] signOut failed", err);
      }});
    }});
    authLine.appendChild(btn);
  }}

  function upsertSubscriberRow() {{
    if (!sbClient || !sbAuthUser) return Promise.resolve();
    const prefs = loadWelcomePrefs() || {{}};
    const localStores = Array.isArray(prefs.retailers)
      ? prefs.retailers.map(function(v) {{ return String(v || "").trim(); }}).filter(Boolean)
      : [];
    const localCats = Array.isArray(prefs.categories)
      ? prefs.categories.map(function(v) {{ return String(v || "").trim(); }}).filter(Boolean)
      : [];

    return sbClient
      .from("subscribers")
      .select("stores,categories")
      .eq("user_id", sbAuthUser.id)
      .maybeSingle()
      .then(function(res) {{
        if (res && res.error) throw res.error;
        const existing = res.data || null;
        const serverStores = (existing && Array.isArray(existing.stores))
          ? existing.stores.map(function(v) {{ return String(v || "").trim(); }}).filter(Boolean)
          : [];
        const serverCats = (existing && Array.isArray(existing.categories))
          ? existing.categories.map(function(v) {{ return String(v || "").trim(); }}).filter(Boolean)
          : [];

        // Only send stores/categories when this device has something to keep.
        // Omitting them on upsert leaves existing Supabase values untouched.
        // Never touch weekly_email / deal_alerts / unsubscribed_at here.
        // Those are only set on explicit email sign-up form submit
        // (resubscribeSignedInUser). New rows rely on DB defaults.
        const payload = {{
          user_id: sbAuthUser.id,
          email: sbAuthUser.email || "",
        }};
        if (localStores.length) payload.stores = localStores;
        if (localCats.length) payload.categories = localCats;

        // New/empty device: pull saved prefs down into local welcome storage.
        let prefsChanged = false;
        const nextPrefs = Object.assign({{}}, prefs);
        if (!localStores.length && serverStores.length) {{
          nextPrefs.retailers = serverStores.slice();
          prefsChanged = true;
        }}
        if (!localCats.length && serverCats.length) {{
          nextPrefs.categories = serverCats.slice();
          prefsChanged = true;
        }}
        if (prefsChanged) {{
          if (!nextPrefs.completed) nextPrefs.completed = true;
          saveWelcomePrefs(nextPrefs);
          applyWelcomePrefs(nextPrefs);
        }}

        return sbClient.from("subscribers").upsert(payload, {{ onConflict: "user_id" }});
      }})
      .then(function(res) {{
        if (res && res.error) {{
          console.error("[SnackBuddy] subscribers upsert failed", res.error);
        }}
      }})
      .catch(function(err) {{
        console.error("[SnackBuddy] subscribers upsert failed", err);
      }});
  }}

  function resubscribeSignedInUser() {{
    // Signed-in user submitted an email form again → clear unsubscribe flags.
    if (!sbClient || !sbAuthUser) return Promise.resolve(false);
    return sbClient.from("subscribers").upsert({{
      user_id: sbAuthUser.id,
      email: sbAuthUser.email || "",
      weekly_email: true,
      deal_alerts: true,
      unsubscribed_at: null,
    }}, {{ onConflict: "user_id" }}).then(function(res) {{
      if (res && res.error) throw res.error;
      markEmailSubscribed();
      return true;
    }}).catch(function(err) {{
      console.error("[SnackBuddy] resubscribe failed", err);
      return false;
    }});
  }}

  function pushStarToServer(op, itemId, itemName, enqueueOnFail) {{
    if (!sbClient || !sbAuthUser || !itemId) {{
      return Promise.resolve(false);
    }}
    const name = itemName || getStarName(itemId) || "";
    const shouldEnqueue = enqueueOnFail !== false;
    let req;
    if (op === "upsert") {{
      req = sbClient.from("stars").upsert({{
        user_id: sbAuthUser.id,
        item_id: itemId,
        item_name: name,
      }}, {{ onConflict: "user_id,item_id" }});
    }} else {{
      req = sbClient.from("stars").delete()
        .eq("user_id", sbAuthUser.id)
        .eq("item_id", itemId);
    }}
    return req.then(function(res) {{
      if (res && res.error) throw res.error;
      return true;
    }}).catch(function(err) {{
      console.error("[SnackBuddy] star " + op + " failed", err);
      if (shouldEnqueue) enqueueStarOp(op, itemId, name);
      return false;
    }});
  }}

  function flushPendingStars() {{
    if (!sbClient || !sbAuthUser) return Promise.resolve();
    const q = loadPendingStars();
    if (!q.length) return Promise.resolve();
    // Clear first so concurrent toggles can enqueue fresh ops.
    savePendingStars([]);
    const remaining = [];
    let chain = Promise.resolve();
    q.forEach(function(row) {{
      chain = chain.then(function() {{
        if (!row || !row.item_id) return;
        const op = row.op === "delete" ? "delete" : "upsert";
        // Skip stale ops that no longer match local state.
        if (op === "upsert" && !starredIds.has(row.item_id)) return;
        if (op === "delete" && starredIds.has(row.item_id)) return;
        return pushStarToServer(op, row.item_id, row.item_name, false).then(function(ok) {{
          if (!ok) remaining.push(row);
        }});
      }});
    }});
    return chain.then(function() {{
      if (!remaining.length) return;
      const fresh = loadPendingStars();
      savePendingStars(remaining.concat(fresh));
    }});
  }}

  function mergeStarsWithServer() {{
    if (!sbClient || !sbAuthUser || sbSyncing) return Promise.resolve();
    sbSyncing = true;
    return sbClient.from("stars").select("item_id,item_name").eq("user_id", sbAuthUser.id)
      .then(function(res) {{
        if (res && res.error) throw res.error;
        const rows = Array.isArray(res.data) ? res.data : [];
        const serverIds = new Set();
        rows.forEach(function(row) {{
          const id = row && row.item_id ? String(row.item_id) : "";
          if (!id) return;
          serverIds.add(id);
          if (row.item_name) setStarName(id, row.item_name);
        }});

        const uploads = [];
        starredIds.forEach(function(id) {{
          if (!serverIds.has(id)) {{
            uploads.push(pushStarToServer("upsert", id, getStarName(id)));
          }}
        }});

        let changed = false;
        serverIds.forEach(function(id) {{
          if (!starredIds.has(id)) {{
            starredIds.add(id);
            changed = true;
          }}
        }});
        if (changed) {{
          saveStarredIds();
          syncStarButtons();
          if (myItemsView) applyFiltersAndSort();
          else syncFloatButton();
        }}

        return Promise.all(uploads).then(function() {{
          return flushPendingStars();
        }});
      }})
      .catch(function(err) {{
        console.error("[SnackBuddy] star merge failed", err);
      }})
      .then(function() {{
        sbSyncing = false;
      }});
  }}

  function handleSignedInSession(user) {{
    sbAuthUser = user || null;
    updateAuthLine();
    syncMyItemsNotifyVisibility();
    syncWelcomeEditHint();
    syncFloatButton();
    if (!sbAuthUser) return Promise.resolve();
    markEmailSubscribed();
    dismissEmailAsk(false);
    return upsertSubscriberRow().then(function() {{
      return mergeStarsWithServer();
    }});
  }}

  function initSupabaseAuth() {{
    if (!sbClient) return;
    sbClient.auth.onAuthStateChange(function(event, session) {{
      const user = session && session.user ? session.user : null;
      if (event === "SIGNED_OUT" || !user) {{
        sbAuthUser = null;
        updateAuthLine();
        syncMyItemsNotifyVisibility();
        syncWelcomeEditHint();
        syncFloatButton();
        return;
      }}
      if (event === "TOKEN_REFRESHED" && sbAuthUser && sbAuthUser.id === user.id) {{
        sbAuthUser = user;
        updateAuthLine();
        return;
      }}
      if (event === "SIGNED_IN" || event === "INITIAL_SESSION" || event === "USER_UPDATED") {{
        handleSignedInSession(user);
      }}
    }});
  }}

  function displayRetailerName(name) {{
    // Visitor-facing label only — filter keys / data-retailer stay unchanged.
    const key = String(name || "").toLowerCase().trim();
    if (key === "harris teeter" || (key.indexOf("harris") !== -1 && key.indexOf("teeter") !== -1)) {{
      return "Harris Teeter · Baltimore";
    }}
    return titleize(String(name || ""));
  }}

  function titleRetailer(name) {{
    return displayRetailerName(name);
  }}

  function daysAgoLabel(iso) {{
    if (!iso) return "";
    const d = new Date(String(iso).slice(0, 10) + "T12:00:00");
    if (isNaN(d.getTime())) return String(iso);
    const today = new Date();
    today.setHours(12, 0, 0, 0);
    const diff = Math.round((today.getTime() - d.getTime()) / 86400000);
    if (diff <= 0) return "today";
    if (diff === 1) return "1 day ago";
    return diff + " days ago";
  }}

  function money(n) {{
    const v = Number(n);
    if (!isFinite(v)) return "";
    return "$" + v.toFixed(2);
  }}

  function buildLastSaleLinesHtml(itemId, onSaleRetailers) {{
    const meta = lastSaleItems[itemId];
    const retailers = (meta && meta.retailers) || {{}};
    const keys = Object.keys(retailers);
    const onSale = onSaleRetailers || new Set();
    if (!keys.length && !onSale.size) {{
      return '<div class="sb-last-sale-line">No sale seen since we started tracking.</div>';
    }}
    const allKeys = Array.from(new Set(keys.concat(Array.from(onSale)))).sort();
    return allKeys.map(function(ret) {{
      const label = titleRetailer(ret);
      if (onSale.has(ret)) {{
        return '<div class="sb-last-sale-line is-now">On sale now at ' + label + '</div>';
      }}
      const rec = retailers[ret] || {{}};
      if (rec.last_sale_date) {{
        const priceBit = rec.sale_price != null ? " (" + money(rec.sale_price) + ")" : "";
        return '<div class="sb-last-sale-line">Last on sale at ' + label + " " +
          daysAgoLabel(rec.last_sale_date) + priceBit + "</div>";
      }}
      return '<div class="sb-last-sale-line">No sale seen at ' + label + " since we started tracking.</div>";
    }}).join("");
  }}

  function syncStarButtons() {{
    document.querySelectorAll("[data-item-id] .sb-star-btn").forEach(function(btn) {{
      const host = btn.closest("[data-item-id]");
      const id = host && host.dataset.itemId;
      const on = !!(id && starredIds.has(id));
      btn.classList.toggle("is-starred", on);
      btn.setAttribute("aria-pressed", on ? "true" : "false");
      btn.setAttribute("aria-label", on ? "Remove from My Items" : "Save to My Items");
      btn.title = on ? "Remove from My Items" : "Save to My Items";
    }});
  }}

  function clearLastSaleLines(card) {{
    const box = card.querySelector(".sb-last-sale-lines");
    if (!box) return;
    box.hidden = true;
    box.innerHTML = "";
    const wrap = box.closest(".sb-deal-history");
    if (wrap) {{
      wrap.hidden = true;
      wrap.open = false;
    }}
  }}

  function fillLastSaleLines(card) {{
    const box = card.querySelector(".sb-last-sale-lines");
    if (!box) return;
    if (!myItemsView) {{
      clearLastSaleLines(card);
      return;
    }}
    const id = card.dataset.itemId || "";
    const onSale = new Set();
    if (card.dataset.onSale === "1" && card.dataset.retailer) {{
      onSale.add(card.dataset.retailer);
    }}
    // Also mark other live cards with same item id
    cards.forEach(function(c) {{
      if (c.dataset.itemId === id && c.dataset.onSale === "1" && c.dataset.retailer) {{
        onSale.add(c.dataset.retailer);
      }}
    }});
    box.innerHTML = buildLastSaleLinesHtml(id, onSale);
    box.hidden = false;
    const histWrap = box.closest(".sb-deal-history");
    if (histWrap) histWrap.hidden = false;
  }}

  function ensureOffSaleCard(itemId) {{
    if (offSaleCards.has(itemId)) return offSaleCards.get(itemId);
    const meta = lastSaleItems[itemId];
    if (!meta) return null;
    const card = document.createElement("div");
    card.className = "sb-my-items-offrow";
    card.dataset.itemId = itemId;
    card.dataset.onSale = "0";
    card.dataset.tier = "sale";
    card.dataset.price = "0";
    card.dataset.saved = "0";
    card.dataset.percent = "0";
    card.dataset.brand = String(meta.brand || "").toLowerCase();
    card.dataset.retailer = "";
    card.dataset.section = "";
    card.dataset.category = "";
    card.dataset.search = [meta.brand, meta.product_name].filter(Boolean).join(" ").toLowerCase();
    card.innerHTML =
      '<button type="button" class="sb-star-btn is-starred" aria-label="Remove from My Items" aria-pressed="true" title="Remove from My Items">' +
      '<svg class="sb-star-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">' +
      '<path d="M12 3.6l2.47 5.01 5.53.8-4 3.9.94 5.5L12 16.9l-4.94 2.6.94-5.5-4-3.9 5.53-.8L12 3.6z"/></svg></button>' +
      '<div class="sb-offrow-main">' +
      '<div class="card-title"></div>' +
      '<div class="sb-offrow-sub"></div>' +
      '<details class="sb-deal-history" hidden><summary>See deal history <span class="sb-hist-chev">▾</span></summary>' +
      '<div class="sb-last-sale-lines"></div></details>' +
      '</div>';
    card.querySelector(".card-title").textContent = meta.product_name || meta.brand || "Saved item";
    offSaleCards.set(itemId, card);
    return card;
  }}

  function updateOffSaleRow(row, itemId) {{
    const meta = lastSaleItems[itemId] || {{}};
    const titleEl = row.querySelector(".card-title");
    const name = meta.product_name || meta.brand || getStarName(itemId) || "Saved item";
    if (titleEl && titleEl.textContent !== name) titleEl.textContent = name;
    const sub = row.querySelector(".sb-offrow-sub");
    if (!sub) return;
    let text = "Not on sale right now";
    const pick = pickSavingsForItem(itemId, getSavingsStores());
    if (pick && pick.sale_weeks > 0 && pick.avg_pct_off !== null) {{
      if (pick.avg_weeks_between !== null && pick.avg_weeks_between <= 1.5) {{
        text += " · usually on sale most weeks, ~" + pick.avg_pct_off + "% off";
      }} else {{
        text += " · usually ~" + pick.avg_pct_off + "% off";
        if (pick.avg_weeks_between !== null) {{
          const wks = Math.max(1, Math.round(pick.avg_weeks_between));
          text += wks === 1 ? " about every week" : (" about every " + wks + " weeks");
        }}
      }}
    }}
    sub.textContent = text;
  }}

  /* ============================
     SAVINGS (My Items) — stores, today's live savings, 8-week history
     ============================ */
  // Bowling Green defaults — never include Harris Teeter (Baltimore) unless the visitor picks it.
  const BG_DEFAULT_STORES = ["walmart", "target", "kroger", "meijer"];
  const SAVINGS_DEFAULT_STORES = BG_DEFAULT_STORES;

  function normStoreKey(s) {{
    return String(s || "").toLowerCase().trim();
  }}

  function isHarrisTeeterKey(s) {{
    const k = normStoreKey(s);
    return k.indexOf("harris") !== -1 && k.indexOf("teeter") !== -1;
  }}

  function getActiveStoreFilter() {{
    // Explicit My-stores selection wins (may include Harris Teeter).
    // With nothing saved/selected, All Deals is BG-only (no HT).
    if (selected.retailer.size > 0) return Array.from(selected.retailer);
    return BG_DEFAULT_STORES.slice();
  }}

  function cardMatchesActiveStores(retailer) {{
    const key = normStoreKey(retailer);
    const stores = getActiveStoreFilter().map(normStoreKey);
    if (!stores.length) return true;
    return stores.indexOf(key) !== -1;
  }}

  function getSavingsStores() {{
    const pref = getPreferredStoreList().map(normStoreKey).filter(Boolean);
    return pref.length ? pref : SAVINGS_DEFAULT_STORES.slice();
  }}

  function fmtSavingsDollars(n) {{
    const v = Number(n) || 0;
    return v >= 1 ? ("$" + Math.round(v)) : ("$" + v.toFixed(2));
  }}

  function extractEndsText(card) {{
    const el = card.querySelector('[class*="availability"]');
    const txt = el ? String(el.textContent || "") : "";
    const m = txt.match(/ends\\s+[A-Za-z]{{3}}\\s+\\d{{1,2}}\\/\\d{{1,2}}/i);
    return m ? m[0] : "";
  }}

  // savings.json -> best retailer pick for one item (null = not in file / too new).
  function pickSavingsForItem(itemId, stores) {{
    const byStore = savingsByItem[itemId];
    if (!byStore || typeof byStore !== "object") return null;
    const allowed = new Set((Array.isArray(stores) ? stores : []).map(normStoreKey));
    function toPick(store, rec) {{
      if (!rec || typeof rec !== "object") return null;
      const weeks = Number(rec.sale_weeks_8wk);
      const total = Number(rec.total_8wk_savings);
      const pctRaw = rec.avg_pct_off;
      let pct = (pctRaw === null || pctRaw === undefined || pctRaw === "") ? null : Number(pctRaw);
      if (pct !== null && !isFinite(pct)) pct = null;
      if (pct !== null) pct = Math.round(pct <= 1 ? pct * 100 : pct);
      const gapRaw = rec.avg_weeks_between_sales;
      let gap = (gapRaw === null || gapRaw === undefined || gapRaw === "") ? null : Number(gapRaw);
      if (gap !== null && (!isFinite(gap) || gap <= 0)) gap = null;
      return {{
        store: store,
        sale_weeks: isFinite(weeks) ? weeks : 0,
        total: isFinite(total) ? total : 0,
        avg_pct_off: pct,
        avg_weeks_between: gap,
      }};
    }}
    function better(a, b) {{
      if (!a) return b;
      if (!b) return a;
      if (b.total !== a.total) return b.total > a.total ? b : a;
      return b.sale_weeks > a.sale_weeks ? b : a;
    }}
    let best = null;
    Object.keys(byStore).forEach(function(store) {{
      if (!allowed.has(normStoreKey(store))) return;
      best = better(best, toPick(store, byStore[store]));
    }});
    if (best) return best;
    // None of the visitor's stores have data — fall back to best overall,
    // but never Harris Teeter unless it is one of their stores.
    Object.keys(byStore).forEach(function(store) {{
      if (isHarrisTeeterKey(store) && !allowed.has(normStoreKey(store))) return;
      best = better(best, toPick(store, byStore[store]));
    }});
    return best;
  }}

  // Live deals on the page for starred items, restricted to the savings stores.
  function computeTodaySavings() {{
    const stores = new Set(getSavingsStores());
    const byItem = new Map();
    cards.forEach(function(card) {{
      const id = card.dataset.itemId || "";
      if (!id || !starredIds.has(id) || card.dataset.onSale !== "1") return;
      const r = normStoreKey(card.dataset.retailer);
      if (!stores.has(r)) return;
      const price = parseFloat(card.dataset.price || "0") || 0;
      let saved = parseFloat(card.dataset.saved || "0") || 0;
      if (saved <= 0) {{
        const oldEl = card.querySelector(".old-price");
        const oldVal = oldEl ? parseFloat(String(oldEl.textContent || "").replace(/[^0-9.]/g, "")) : NaN;
        if (isFinite(oldVal) && oldVal > price) saved = oldVal - price;
      }}
      if (!byItem.has(id)) byItem.set(id, []);
      byItem.get(id).push({{ card: card, retailer: r, price: price, saved: saved }});
    }});
    let total = 0;
    byItem.forEach(function(list) {{
      list.sort(function(a, b) {{
        if (a.saved !== b.saved) return b.saved - a.saved;
        return a.price - b.price;
      }});
      if (list[0].saved > 0) total += list[0].saved;
    }});
    return {{ total: total, onSaleCount: byItem.size, byItem: byItem }};
  }}

  function getItemDisplayName(itemId) {{
    const n = getStarName(itemId);
    if (n) return n;
    for (let i = 0; i < cards.length; i += 1) {{
      if (cards[i].dataset.itemId === itemId) {{
        const t = cards[i].querySelector(".card-title");
        if (t && t.textContent.trim()) return t.textContent.trim();
      }}
    }}
    return "Saved item";
  }}

  /* ---- My Items compact card decoration ---- */
  function ensureMiExtras(card) {{
    const content = card.querySelector(".card-content");
    if (!content) return null;
    let store = content.querySelector(".sb-mi-store");
    let also = content.querySelector(".sb-mi-also");
    if (!store || !also) {{
      store = document.createElement("p");
      store.className = "sb-mi-store sb-mi-extra";
      also = document.createElement("p");
      also.className = "sb-mi-also sb-mi-extra";
      const anchor = content.querySelector(".sb-deal-history") || content.querySelector(".card-footer");
      if (anchor) {{
        content.insertBefore(store, anchor);
        content.insertBefore(also, anchor);
      }} else {{
        content.appendChild(store);
        content.appendChild(also);
      }}
    }}
    return {{ store: store, also: also }};
  }}

  function setCardCompact(card, on, alsoEntry) {{
    const cta = card.querySelector("a.view-button");
    if (cta) {{
      if (on) {{
        if (cta.dataset.origCta === undefined) cta.dataset.origCta = cta.textContent;
        cta.textContent = "See deal";
      }} else if (cta.dataset.origCta !== undefined) {{
        cta.textContent = cta.dataset.origCta;
        delete cta.dataset.origCta;
      }}
    }}
    if (!on) return;
    const ex = ensureMiExtras(card);
    if (!ex) return;
    const ends = extractEndsText(card);
    ex.store.textContent = "at " + displayRetailerName(card.dataset.retailer) + (ends ? " · " + ends : "");
    if (alsoEntry) {{
      ex.also.textContent = "Also on sale at " + displayRetailerName(alsoEntry.retailer) + ": " + money(alsoEntry.price);
      ex.also.hidden = false;
    }} else {{
      ex.also.textContent = "";
      ex.also.hidden = true;
    }}
  }}

  /* ---- My Items slim top line ---- */
  function renderMyItemsTodayLine(today) {{
    if (!myItemsTodayLine) return;
    const show = myItemsView && starredIds.size > 0 && today.total > 0;
    myItemsTodayLine.classList.toggle("is-visible", show);
    if (show) myItemsTodayLine.textContent = "Save " + fmtSavingsDollars(today.total) + " today on your favorites ›";
  }}

  /* ---- Savings slide-out panel ---- */
  function renderSavingsPanel() {{
    if (!savingsPanel || !savingsPanel.classList.contains("is-on")) return;
    const total = starredIds.size;

    if (savingsLoadStatus === "error") {{
      savingsPanelHeadline.textContent = "Savings details are unavailable right now.";
      savingsPanelOnSale.textContent = "Try refreshing in a bit.";
      savingsPanelEight.hidden = true;
      if (savingsPanelQty) savingsPanelQty.hidden = true;
      if (savingsPanelHow) savingsPanelHow.hidden = true;
      return;
    }}
    savingsPanelEight.hidden = false;
    if (savingsPanelHow) savingsPanelHow.hidden = false;

    const today = computeTodaySavings();
    savingsPanelHeadline.textContent = today.total > 0
      ? ("Your favorites on sale today: save " + fmtSavingsDollars(today.total))
      : "None of your favorites are on sale today.";
    savingsPanelOnSale.textContent = total === 1
      ? (today.onSaleCount > 0 ? "Your favorite is on sale right now." : "Your favorite isn’t on sale right now.")
      : (today.onSaleCount + " of your " + total + " favorites are on sale right now.");

    if (savingsLoadStatus === "pending") {{
      savingsPanelEight.textContent = "Calculating how often your favorites go on sale…";
      if (savingsPanelQty) savingsPanelQty.hidden = true;
      if (savingsPanelList) savingsPanelList.innerHTML = "";
      return;
    }}

    const stores = getSavingsStores();
    let count = 0;
    let sum = 0;
    let withData = 0;
    const rows = [];
    Array.from(starredIds).forEach(function(itemId) {{
      const pick = pickSavingsForItem(itemId, stores);
      rows.push({{ id: itemId, name: getItemDisplayName(itemId), pick: pick }});
      if (!pick) return;
      withData += 1;
      count += pick.sale_weeks;
      sum += pick.total;
    }});

    const qty = savingsBuyQty;
    if (withData === 0) {{
      savingsPanelEight.textContent = "Your favorites are too new to tell yet — check back soon.";
    }} else if (count === 0) {{
      savingsPanelEight.textContent = "In the last 2 months, none of your favorites went on sale.";
    }} else {{
      savingsPanelEight.textContent =
        "In the last 2 months, your favorites went on sale " + count + (count === 1 ? " time" : " times") +
        " — worth " + fmtSavingsDollars(sum * qty) + " if you’d caught each one" +
        (qty > 1 ? " (buying " + qty + " each time)" : "") + ".";
    }}
    if (savingsPanelQty) {{
      savingsPanelQty.hidden = !(count > 0 && sum > 0);
      savingsPanelQty.querySelectorAll(".sb-savings-qty-btn").forEach(function(btn) {{
        const on = Number(btn.getAttribute("data-qty")) === qty;
        btn.classList.toggle("is-active", on);
        btn.setAttribute("aria-pressed", on ? "true" : "false");
      }});
    }}

    if (savingsPanelList) {{
      rows.sort(function(a, b) {{ return a.name.localeCompare(b.name); }});
      savingsPanelList.innerHTML = "";
      rows.forEach(function(row) {{
        const li = document.createElement("li");
        let text;
        if (!row.pick) {{
          text = row.name + " — too new to tell yet";
        }} else if (row.pick.sale_weeks <= 0) {{
          text = row.name + " — no sales in 2 months";
        }} else {{
          text = row.name + " — on sale " + row.pick.sale_weeks + "× in 2 months" +
            (row.pick.avg_pct_off !== null ? (", usually ~" + row.pick.avg_pct_off + "% off") : "");
        }}
        li.textContent = text;
        savingsPanelList.appendChild(li);
      }});
    }}
  }}

  function openSavingsPanel() {{
    if (!savingsPanel || !myItemsView || starredIds.size === 0) return;
    savingsPanel.hidden = false;
    void savingsPanel.offsetWidth;
    savingsPanel.classList.add("is-on");
    renderSavingsPanel();
    syncFloatButton();
    trackEvent("savings_panel_open", {{}});
  }}

  function closeSavingsPanel() {{
    if (!savingsPanel) return;
    const wasOn = savingsPanel.classList.contains("is-on");
    savingsPanel.classList.remove("is-on");
    window.setTimeout(function() {{
      if (savingsPanel && !savingsPanel.classList.contains("is-on")) savingsPanel.hidden = true;
    }}, 300);
    if (wasOn) syncFloatButton();
  }}

  function wireSavingsPanel() {{
    if (savingsPanelClose) savingsPanelClose.addEventListener("click", closeSavingsPanel);
    if (myItemsTodayLine) myItemsTodayLine.addEventListener("click", openSavingsPanel);
    if (savingsPanelQty) {{
      savingsPanelQty.addEventListener("click", function(ev) {{
        const btn = ev.target && ev.target.closest ? ev.target.closest(".sb-savings-qty-btn") : null;
        if (!btn) return;
        const n = parseInt(btn.getAttribute("data-qty") || "1", 10);
        savingsBuyQty = (n >= 1 && n <= 3) ? n : 1;
        renderSavingsPanel();
      }});
    }}
    document.addEventListener("keydown", function(ev) {{
      if (ev.key === "Escape" && savingsPanel && savingsPanel.classList.contains("is-on")) closeSavingsPanel();
    }});
    document.addEventListener("click", function(ev) {{
      if (!savingsPanel || !savingsPanel.classList.contains("is-on")) return;
      const t = ev.target;
      if (!t || !t.closest) return;
      if (savingsPanel.contains(t) || t.closest("#sb-my-items-today-line, #sb-email-ask-pill")) return;
      closeSavingsPanel();
    }});
  }}

  function toggleStar(itemId, itemName) {{
    if (!itemId) return;
    const name = String(itemName || "").trim() || getStarName(itemId);
    let adding = false;
    if (starredIds.has(itemId)) {{
      starredIds.delete(itemId);
      trackEvent("star_remove", {{ item_id: itemId, item_name: name || "" }});
    }} else {{
      adding = true;
      starredIds.add(itemId);
      setStarName(itemId, name);
      trackEvent("star_add", {{ item_id: itemId, item_name: name || "" }});
    }}
    saveStarredIds();
    syncStarButtons();
    if (myItemsView) applyFiltersAndSort();
    syncFloatButton();
    if (sbAuthUser) {{
      pushStarToServer(adding ? "upsert" : "delete", itemId, name);
      if (adding) showStarSavedToast();
    }} else if (adding) {{
      maybeAskEmailAfterStar();
    }}
  }}

  function bindStarClicks(root) {{
    if (!root) return;
    root.addEventListener("click", function(ev) {{
      const btn = ev.target && ev.target.closest ? ev.target.closest(".sb-star-btn") : null;
      if (!btn || !root.contains(btn)) return;
      ev.preventDefault();
      ev.stopPropagation();
      const host = btn.closest("[data-item-id]");
      if (!host) return;
      const nameEl = host.querySelector(".card-title, .sb-savings-tile-name, .sb-catalog-name");
      toggleStar(host.dataset.itemId || "", nameEl ? nameEl.textContent.trim() : "");
    }});
  }}
  bindStarClicks(grid);
  const bestSavings = document.getElementById("sb-best-savings");
  bindStarClicks(bestSavings);
  if (bestSavings) {{
    bestSavings.addEventListener("click", function(ev) {{
      if (ev.target && ev.target.closest && ev.target.closest(".sb-star-btn")) return;
      const tile = ev.target && ev.target.closest ? ev.target.closest(".sb-savings-tile") : null;
      if (!tile || !bestSavings.contains(tile)) return;
      const url = tile.getAttribute("data-url") || "";
      if (url) window.open(url, "_blank", "noopener,noreferrer");
    }});
    bestSavings.addEventListener("keydown", function(ev) {{
      if (ev.key !== "Enter" && ev.key !== " ") return;
      const tile = ev.target && ev.target.closest ? ev.target.closest(".sb-savings-tile") : null;
      if (!tile || ev.target.closest(".sb-star-btn")) return;
      ev.preventDefault();
      const url = tile.getAttribute("data-url") || "";
      if (url) window.open(url, "_blank", "noopener,noreferrer");
    }});
  }}

  let storesPopoverOpen = false;

  function cardMatchesTier(card) {{
    if (myItemsView) return true;
    if (selectedTiers.size === 0) return true;
    const t = card.dataset.tier || "sale";
    return selectedTiers.has(t);
  }}

  // Number of active filters that live inside the Filters sheet (sort, section, brand, tiers).
  function sheetFilterCount() {{
    const sortMode = sortSelect ? (sortSelect.value || "best") : "best";
    return (sortMode !== "best" ? 1 : 0)
      + selected.section.size + selected.brand.size + selectedTiers.size;
  }}

  function categoryChipLabel(val) {{
    return CATEGORY_CHIP_LABELS[String(val || "").toLowerCase()] || titleize(val);
  }}

  function categoryChoices() {{
    const present = uniq(cards.map(function(c) {{ return c.dataset.category; }}));
    const ordered = CATEGORY_PREFERRED.filter(function(c) {{ return present.indexOf(c) !== -1; }});
    present.forEach(function(c) {{
      if (ordered.indexOf(c) === -1) ordered.push(c);
    }});
    return ordered;
  }}

  function buildCategoryChips() {{
    if (!categoryChipsBox) return;
    categoryChipsBox.innerHTML = "";
    const opts = [{{ value: "", label: "All" }}].concat(categoryChoices().map(function(v) {{
      return {{ value: v, label: categoryChipLabel(v) }};
    }}));
    opts.forEach(function(opt) {{
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "sb-chip sb-cat-chip";
      btn.dataset.category = opt.value;
      btn.setAttribute("aria-pressed", "false");
      btn.textContent = opt.label;
      btn.addEventListener("click", function() {{
        setCategorySelection(opt.value);
      }});
      categoryChipsBox.appendChild(btn);
    }});
    syncCategoryChips();
  }}

  function syncCategoryChips() {{
    if (!categoryChipsBox) return;
    const multi = selected.category.size > 1;
    const cur = selected.category.size === 1 ? Array.from(selected.category)[0] : "";
    categoryChipsBox.querySelectorAll(".sb-cat-chip").forEach(function(el) {{
      const v = el.dataset.category || "";
      const active = !multi && v === cur;
      el.classList.toggle("is-active", active);
      el.setAttribute("aria-pressed", active ? "true" : "false");
    }});
  }}

  function setCategorySelection(value) {{
    selected.category.clear();
    if (value) {{
      selected.category.add(value);
      trackEvent("filter_apply", {{
        filter_type: "category",
        filter_value: value
      }});
    }}
    // Picking a category leaves the My Items view.
    myItemsView = false;
    syncTierChips();
    applyFiltersAndSort();
    refreshFilterOptionLists();
  }}

  function updateStoresChip() {{
    if (!storesChip || !storesChipLabel) return;
    const stores = Array.from(selected.retailer);
    let label = "My stores";
    if (stores.length === 1) label = displayRetailerName(stores[0]);
    else if (stores.length > 1) label = stores.length + " stores";
    storesChipLabel.textContent = label;
    storesChip.classList.toggle("is-active", stores.length > 0 && !storesPopoverOpen);
    storesChip.title = stores.length
      ? ("Showing: " + stores.map(displayRetailerName).join(", "))
      : "Showing all stores";
    if (typeof window.__sbRefreshCatalog === "function") window.__sbRefreshCatalog();
  }}

  function syncTierChips() {{
    document.querySelectorAll("input[data-tier-check]").forEach(function(cb) {{
      cb.checked = !myItemsView && selectedTiers.has(cb.value);
    }});
    if (myItemsExitChip) myItemsExitChip.hidden = !myItemsView;
    syncCategoryChips();
    updateBadges();
    updateChipRowScroll();
  }}

  function setTierSelection(mode) {{
    if (mode === "my-items") {{
      myItemsView = !myItemsView;
      if (myItemsView) {{
        selectedTiers.clear();
        trackEvent("my_items_open", {{}});
        maybeAskEmailOnMyItemsOpen();
      }}
      syncTierChips();
      applyFiltersAndSort();
      return;
    }}
    myItemsView = false;
    if (mode === "all") {{
      selectedTiers.clear();
    }} else if (selectedTiers.has(mode)) {{
      selectedTiers.delete(mode);
    }} else {{
      selectedTiers.add(mode);
      trackEvent("filter_apply", {{
        filter_type: "tier",
        filter_value: mode
      }});
    }}
    syncTierChips();
    applyFiltersAndSort();
  }}

  function cardMatchesSelected(card, excludeKey) {{
    const r = card.dataset.retailer;
    const s = card.dataset.section;
    const c = card.dataset.category;
    const b = card.dataset.brand;

    if (!cardMatchesTier(card)) return false;
    // My Items ignores the store filter for visibility counts too.
    if (!myItemsView && excludeKey !== "retailer" && !cardMatchesActiveStores(r)) return false;
    if (excludeKey !== "section" && selected.section.size > 0 && !selected.section.has(s)) return false;
    if (excludeKey !== "category" && selected.category.size > 0 && !selected.category.has(c)) return false;
    if (excludeKey !== "brand" && selected.brand.size > 0 && !selected.brand.has(b)) return false;
    return true;
  }}

  function getSmartOptionsForKey(key) {{
    const matching = cards.filter(function(card) {{ return cardMatchesSelected(card, key); }});

    return uniq(matching.map(function(card) {{ return card.dataset[key]; }})).map(function(v) {{
      return {{
        value: v,
        label: key === "retailer" ? displayRetailerName(v) : titleize(v),
      }};
    }});
  }}

  // Only sheet filters (section/brand) are pruned; stores + category are explicit chip choices.
  function pruneInvalidSelections() {{
    SHEET_DEFS.forEach(function(def) {{
      const available = new Set(getSmartOptionsForKey(def.key).map(function(opt) {{ return opt.value; }}));
      Array.from(selected[def.key]).forEach(function(val) {{
        if (!available.has(val)) selected[def.key].delete(val);
      }});
    }});
  }}

  function updateBadges() {{
    const total = sheetFilterCount();
    if (filterMasterCount) {{
      if (total > 0) {{
        filterMasterCount.textContent = String(total);
        filterMasterCount.hidden = false;
      }} else {{
        filterMasterCount.hidden = true;
      }}
    }}
    filterMasterBtn?.classList.toggle("is-active", total > 0 && !(filterSheet?.classList.contains("open")));
    syncCategoryChips();
    updateStoresChip();
  }}

  function syncCheckboxes(key) {{
    document.querySelectorAll('input[data-filter-key="' + key + '"]').forEach(function(cb) {{
      cb.checked = selected[key].has(cb.value);
    }});
  }}

  function syncAllCheckboxes() {{
    FILTER_DEFS.forEach(function(def) {{ syncCheckboxes(def.key); }});
  }}

  /* ---------- My stores popover ---------- */
  function storeChoices() {{
    const present = uniq(cards.map(function(c) {{ return c.dataset.retailer; }}));
    const ordered = RETAILER_ORDER.filter(function(r) {{ return present.indexOf(r) !== -1; }});
    present.forEach(function(r) {{
      if (ordered.indexOf(r) === -1) ordered.push(r);
    }});
    return ordered;
  }}

  function persistStoreSelection() {{
    const existing = loadWelcomePrefs() || {{ completed: true, categories: [] }};
    const next = Object.assign({{}}, existing, {{
      completed: true,
      retailers: Array.from(selected.retailer),
    }});
    saveWelcomePrefs(next);
  }}

  function onStoreToggle(value, checked) {{
    if (checked) {{
      selected.retailer.add(value);
      trackEvent("filter_apply", {{
        filter_type: "retailer",
        filter_value: value
      }});
    }} else {{
      selected.retailer.delete(value);
    }}
    persistStoreSelection();
    syncCheckboxes("retailer");
    updateBadges();
    applyFiltersAndSort();
    refreshFilterOptionLists();
    updateStoresLine();
  }}

  function buildStoresPopover() {{
    if (!storesPopover) return;
    storesPopover.innerHTML = "";
    const title = document.createElement("div");
    title.className = "sb-popover-title";
    title.textContent = "My stores";
    storesPopover.appendChild(title);
    storeChoices().forEach(function(val) {{
      const label = document.createElement("label");
      label.className = "sb-filter-check";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = val;
      cb.dataset.filterKey = "retailer";
      cb.checked = selected.retailer.has(val);
      cb.addEventListener("change", function() {{
        onStoreToggle(val, cb.checked);
      }});
      const span = document.createElement("span");
      span.textContent = displayRetailerName(val);
      label.appendChild(cb);
      label.appendChild(span);
      storesPopover.appendChild(label);
    }});
    const footer = document.createElement("div");
    footer.className = "sb-popover-footer";
    const allBtn = document.createElement("button");
    allBtn.type = "button";
    allBtn.className = "sb-popover-link";
    allBtn.textContent = "Show all stores";
    allBtn.addEventListener("click", function() {{
      selected.retailer.clear();
      persistStoreSelection();
      syncCheckboxes("retailer");
      updateBadges();
      applyFiltersAndSort();
      refreshFilterOptionLists();
      updateStoresLine();
    }});
    const done = document.createElement("button");
    done.type = "button";
    done.className = "sb-popover-done";
    done.textContent = "Done";
    done.addEventListener("click", closeStoresPopover);
    footer.appendChild(allBtn);
    footer.appendChild(done);
    storesPopover.appendChild(footer);
  }}

  function positionStoresPopover() {{
    if (!storesPopover || !storesChip) return;
    const rect = storesChip.getBoundingClientRect();
    const panelWidth = storesPopover.offsetWidth;
    const panelHeight = storesPopover.offsetHeight;
    let left = Math.max(8, Math.min(rect.left, window.innerWidth - panelWidth - 8));
    let top = rect.bottom + 6;
    if (top + panelHeight > window.innerHeight - 8) {{
      top = Math.max(8, rect.top - panelHeight - 6);
    }}
    storesPopover.style.left = left + "px";
    storesPopover.style.top = top + "px";
  }}

  function openStoresPopover() {{
    if (!storesPopover || !storesChip) return;
    closeFilterSheet();
    buildStoresPopover();
    storesPopover.hidden = false;
    storesPopoverOpen = true;
    storesChip.setAttribute("aria-expanded", "true");
    positionStoresPopover();
    filterDropdownBackdrop?.classList.add("open");
    updateStoresChip();
  }}

  function closeStoresPopover() {{
    if (!storesPopoverOpen && (!storesPopover || storesPopover.hidden)) return;
    storesPopoverOpen = false;
    if (storesPopover) {{
      storesPopover.hidden = true;
      storesPopover.innerHTML = "";
    }}
    storesChip?.setAttribute("aria-expanded", "false");
    filterDropdownBackdrop?.classList.remove("open");
    updateStoresChip();
  }}

  /* ---------- Filters sheet ---------- */
  function openFilterSheet() {{
    closeStoresPopover();
    buildFilterSheet();
    filterSheet?.classList.add("open");
    filterSheetBackdrop?.classList.add("open");
    filterMasterBtn?.setAttribute("aria-expanded", "true");
    updateBadges();
  }}

  function closeFilterSheet() {{
    filterSheet?.classList.remove("open");
    filterSheetBackdrop?.classList.remove("open");
    filterMasterBtn?.setAttribute("aria-expanded", "false");
    updateBadges();
  }}

  function onFilterChange(key, value, checked) {{
    if (checked) selected[key].add(value);
    else selected[key].delete(value);
    if (checked) {{
      trackEvent("filter_apply", {{
        filter_type: key,
        filter_value: value
      }});
    }}
    pruneInvalidSelections();
    syncAllCheckboxes();
    updateBadges();
    applyFiltersAndSort();
    refreshFilterOptionLists();
  }}

  function buildCheckboxList(container, key) {{
    container.innerHTML = "";
    const options = getSmartOptionsForKey(key);
    if (!options.length) {{
      const empty = document.createElement("div");
      empty.className = "sb-filter-dropdown-empty";
      empty.textContent = "No options match your current filters.";
      container.appendChild(empty);
      return;
    }}
    options.forEach(function(opt) {{
      const label = document.createElement("label");
      label.className = "sb-filter-check";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = opt.value;
      cb.dataset.filterKey = key;
      cb.checked = selected[key].has(opt.value);
      cb.addEventListener("change", function() {{
        onFilterChange(key, opt.value, cb.checked);
      }});
      const span = document.createElement("span");
      span.textContent = opt.label;
      label.appendChild(cb);
      label.appendChild(span);
      container.appendChild(label);
    }});
  }}

  function refreshFilterOptionLists() {{
    if (filterSheet?.classList.contains("open")) {{
      buildFilterSheet();
    }}
  }}

  function makeSheetGroup(titleText) {{
    const group = document.createElement("div");
    group.className = "sb-filter-sheet-group";
    const title = document.createElement("div");
    title.className = "sb-filter-sheet-group-title";
    title.textContent = titleText;
    group.appendChild(title);
    return group;
  }}

  function buildFilterSheet() {{
    if (!filterSheetBody) return;
    const prevScroll = filterSheetBody.scrollTop;
    filterSheetBody.innerHTML = "";

    // Sort (kept in sync with the hidden #sb-sort-select)
    const sortGroup = makeSheetGroup("Sort");
    const currentSort = sortSelect ? (sortSelect.value || "best") : "best";
    SORT_OPTIONS.forEach(function(opt) {{
      const label = document.createElement("label");
      label.className = "sb-filter-check";
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "sb-sheet-sort";
      radio.value = opt.value;
      radio.checked = currentSort === opt.value;
      radio.addEventListener("change", function() {{
        if (!radio.checked || !sortSelect) return;
        sortSelect.value = opt.value;
        sortSelect.dispatchEvent(new Event("change", {{ bubbles: true }}));
      }});
      const span = document.createElement("span");
      span.textContent = opt.label;
      label.appendChild(radio);
      label.appendChild(span);
      sortGroup.appendChild(label);
    }});
    filterSheetBody.appendChild(sortGroup);

    // Section + Brand
    SHEET_DEFS.forEach(function(def) {{
      const group = makeSheetGroup(def.label);
      const body = document.createElement("div");
      if (def.key === "brand") body.className = "sb-filter-sheet-scroll";
      buildCheckboxList(body, def.key);
      group.appendChild(body);
      filterSheetBody.appendChild(group);
    }});

    // Deal tiers
    const tierGroup = makeSheetGroup("Deal tiers");
    TIER_OPTIONS.forEach(function(opt) {{
      const label = document.createElement("label");
      label.className = "sb-filter-check";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = opt.value;
      cb.dataset.tierCheck = "1";
      cb.checked = !myItemsView && selectedTiers.has(opt.value);
      cb.addEventListener("change", function() {{
        setTierSelection(opt.value);
      }});
      const span = document.createElement("span");
      span.textContent = opt.label;
      label.appendChild(cb);
      label.appendChild(span);
      tierGroup.appendChild(label);
    }});
    filterSheetBody.appendChild(tierGroup);
    filterSheetBody.scrollTop = prevScroll;
  }}

  function updateChipRowScroll() {{
    if (!chipRowTrack || !chipRowScroll) return;
    const maxScroll = chipRowTrack.scrollWidth - chipRowTrack.clientWidth;
    const show = maxScroll > 4;
    chipRowScroll.classList.toggle("has-scroll-prev", show && chipRowTrack.scrollLeft > 4);
    chipRowScroll.classList.toggle("has-scroll-next", show && chipRowTrack.scrollLeft < maxScroll - 4);
  }}

  function applyFiltersAndSort() {{
    pruneInvalidSelections();
    const selR = selected.retailer;
    const selS = selected.section;
    const selC = selected.category;
    const selB = selected.brand;
    const q = (searchInput?.value || "").trim().toLowerCase();
    const sortMode = (sortSelect?.value || "best");
    let shown = [];

    if (searchWrap) searchWrap.classList.toggle("has-value", !!q);
    if (q && searchWrap && searchWrap.hidden) setSearchExpanded(true, false);
    grid.classList.toggle("sb-my-items-compact", myItemsView);
    updateAuthLine();
    syncMyItemsNotifyVisibility();
    if (myItemsEmpty) myItemsEmpty.classList.remove("is-visible");

    // Hide synthetic off-sale cards unless My Items view is on
    offSaleCards.forEach(function(card) {{
      card.style.display = "none";
      if (card.parentNode) card.parentNode.removeChild(card);
    }});

    const liveOnSaleIds = new Set();
    cards.forEach(function(card) {{
      if (card.dataset.itemId) liveOnSaleIds.add(card.dataset.itemId);
    }});

    // My Items: ignore store filter; dedupe by item id preferring the visitor's stores.
    const seenItemIds = new Set();
    const myItemsBest = myItemsView ? new Map() : null;

    cards.forEach(function(card) {{
      const r = card.dataset.retailer;
      const s = card.dataset.section;
      const c = card.dataset.category;
      const b = card.dataset.brand;
      const hay = (card.dataset.search || "").toLowerCase();
      const itemId = card.dataset.itemId || "";

      // My Items always includes every store; All Deals uses saved stores or BG defaults (no HT).
      const matchR = myItemsView || cardMatchesActiveStores(r);
      const matchS = selS.size === 0 || selS.has(s);
      const matchC = selC.size === 0 || selC.has(c);
      const matchB = selB.size === 0 || selB.has(b);
      const matchQ = !q || hay.indexOf(q) !== -1;
      const matchT = cardMatchesTier(card);
      const matchStar = !myItemsView || (itemId && starredIds.has(itemId));

      if (!(matchR && matchS && matchC && matchB && matchQ && matchT && matchStar)) {{
        if (!(myItemsBest && itemId && myItemsBest.get(itemId) === card)) {{
          card.style.display = "none";
          clearLastSaleLines(card);
        }}
        return;
      }}

      if (myItemsView && itemId && myItemsBest) {{
        const prev = myItemsBest.get(itemId);
        const prevRank = preferredStoreRank(prev ? prev.dataset.retailer : "");
        const curRank = preferredStoreRank(r);
        // Same item at several stores: prefer the visitor's stores, then the bigger saving.
        if (prev && (prevRank < curRank ||
            (prevRank === curRank &&
              (parseFloat(prev.dataset.saved || "0") || 0) >= (parseFloat(card.dataset.saved || "0") || 0)))) {{
          card.style.display = "none";
          return;
        }}
        if (prev) {{
          prev.style.display = "none";
          clearLastSaleLines(prev);
        }}
        myItemsBest.set(itemId, card);
        return;
      }}

      card.style.display = "";
      if (myItemsView && itemId) seenItemIds.add(itemId);
      fillLastSaleLines(card);
      shown.push(card);
    }});

    const today = computeTodaySavings();
    if (myItemsBest) {{
      myItemsBest.forEach(function(card, itemId) {{
        card.style.display = "";
        fillLastSaleLines(card);
        const shownStore = normStoreKey(card.dataset.retailer);
        const entries = today.byItem.get(itemId) || [];
        let also = null;
        for (let i = 0; i < entries.length; i += 1) {{
          if (entries[i].retailer !== shownStore) {{ also = entries[i]; break; }}
        }}
        setCardCompact(card, true, also);
        shown.push(card);
        if (itemId) seenItemIds.add(itemId);
      }});
    }} else {{
      cards.forEach(function(card) {{ setCardCompact(card, false); }});
    }}

    if (myItemsView) {{
      Array.from(starredIds).forEach(function(itemId) {{
        if (seenItemIds.has(itemId)) return;
        if (liveOnSaleIds.has(itemId)) return; // shown via live card path (or filtered out)
        // Skip ids with no last_sale metadata and not on page
        if (!lastSaleItems[itemId] && !liveOnSaleIds.has(itemId)) return;
        const meta = lastSaleItems[itemId];
        if (!meta) return;
        const hay = [meta.brand, meta.product_name].join(" ").toLowerCase();
        if (q && hay.indexOf(q) === -1) return;
        const card = ensureOffSaleCard(itemId);
        if (!card) return;
        updateOffSaleRow(card, itemId);
        fillLastSaleLines(card);
        card.style.display = "";
        shown.push(card);
        seenItemIds.add(itemId);
      }});
    }}

    renderMyItemsTodayLine(today);

    function getTierPriority(tier) {{
      if (tier === "diamond") return 4;
      if (tier === "fire") return 3;
      if (tier === "strong") return 2;
      return 1;
    }}

    shown.sort(function(a, b) {{
      const aOn = a.dataset.onSale === "1" ? 1 : 0;
      const bOn = b.dataset.onSale === "1" ? 1 : 0;
      if (myItemsView && aOn !== bOn) return bOn - aOn;
      if (myItemsView) {{
        const aStore = preferredStoreRank(a.dataset.retailer);
        const bStore = preferredStoreRank(b.dataset.retailer);
        if (aStore !== bStore) return aStore - bStore;
      }}

      const aPrice = parseFloat(a.dataset.price || "0");
      const bPrice = parseFloat(b.dataset.price || "0");
      const aPct = parseFloat(a.dataset.percent || "0");
      const bPct = parseFloat(b.dataset.percent || "0");
      const aSaved = parseFloat(a.dataset.saved || "0");
      const bSaved = parseFloat(b.dataset.saved || "0");

      if (sortMode === "pct-desc") {{
        if (aPct !== bPct) return bPct - aPct;
        return aPrice - bPrice;
      }}
      if (sortMode === "saved-desc") {{
        if (aSaved !== bSaved) return bSaved - aSaved;
        if (aPct !== bPct) return bPct - aPct;
        return aPrice - bPrice;
      }}
      if (sortMode === "price-asc") {{
        if (aPrice !== bPrice) return aPrice - bPrice;
        return bPct - aPct;
      }}
      if (sortMode === "price-desc") {{
        if (aPrice !== bPrice) return bPrice - aPrice;
        return bPct - aPct;
      }}

      const aTierPriority = getTierPriority(a.dataset.tier || "sale");
      const bTierPriority = getTierPriority(b.dataset.tier || "sale");
      if (aTierPriority !== bTierPriority) return bTierPriority - aTierPriority;
      if (aPct !== bPct) return bPct - aPct;
      return aPrice - bPrice;
    }});

    shown.forEach(function(card) {{ grid.appendChild(card); }});

    const emptyMyItems = myItemsView && starredIds.size === 0;
    const emptyFiltered = shown.length === 0 && !emptyMyItems;
    if (dealsEmpty) dealsEmpty.classList.toggle("is-visible", emptyFiltered && !myItemsView);
    if (myItemsEmpty) myItemsEmpty.classList.toggle("is-visible", emptyMyItems || (myItemsView && shown.length === 0 && starredIds.size > 0));
    if (myItemsView && shown.length === 0 && starredIds.size > 0 && myItemsEmpty) {{
      myItemsEmpty.textContent = "None of your saved items match the current search or filters.";
      myItemsEmpty.classList.add("is-visible");
    }} else if (myItemsEmpty && emptyMyItems) {{
      myItemsEmpty.textContent = "No saved items yet. Tap the ★ on any deal card to save it here — we’ll track when it goes on sale at each store.";
    }}
    if (resultsCount) {{
      if (myItemsView) {{
        resultsCount.textContent = shown.length + (shown.length === 1 ? " saved item" : " saved items");
      }} else {{
        resultsCount.textContent = formatOnSaleCountLine();
      }}
    }}
    syncFloatButton();
    renderSavingsPanel();
  }}

  function clearAllFilters() {{
    // Stores are the visitor's saved default, so "Clear all" leaves them alone.
    selected.section.clear();
    selected.category.clear();
    selected.brand.clear();
    selectedTiers.clear();
    myItemsView = false;
    if (searchInput) searchInput.value = "";
    if (sortSelect) sortSelect.value = "best";
    setSearchExpanded(false, false);
    syncTierChips();
    syncAllCheckboxes();
    updateBadges();
    applyFiltersAndSort();
    refreshFilterOptionLists();
  }}

  let searchTimer = null;
  let searchAnalyticsTimer = null;
  let lastTrackedSearch = "";
  if (searchInput) {{
    searchInput.addEventListener("input", function() {{
      clearTimeout(searchTimer);
      searchTimer = setTimeout(applyFiltersAndSort, 120);
      clearTimeout(searchAnalyticsTimer);
      searchAnalyticsTimer = setTimeout(function() {{
        const term = (searchInput.value || "").trim().toLowerCase();
        if (term.length < 2 || term === lastTrackedSearch) return;
        lastTrackedSearch = term;
        trackEvent("search", {{ search_term: term }});
      }}, 800);
    }});
  }}
  function setSearchExpanded(on, focusInput) {{
    if (searchWrap) searchWrap.hidden = !on;
    if (searchToggle) {{
      searchToggle.setAttribute("aria-expanded", on ? "true" : "false");
      const hasValue = !!(searchInput && String(searchInput.value || "").trim());
      searchToggle.classList.toggle("is-active", on || hasValue);
    }}
    if (on && focusInput && searchInput) searchInput.focus();
  }}

  function closeSearch() {{
    if (searchInput) searchInput.value = "";
    lastTrackedSearch = "";
    clearTimeout(searchAnalyticsTimer);
    setSearchExpanded(false, false);
    applyFiltersAndSort();
  }}

  /* ============================
     CATALOG VIEW
     ============================ */
  const catalogEl = document.getElementById("sb-catalog");
  const catalogBack = document.getElementById("sb-catalog-back");
  const catalogSearch = document.getElementById("sb-catalog-search");
  const catalogChips = document.getElementById("sb-catalog-chips");
  const catalogStoresChip = document.getElementById("sb-catalog-stores-chip");
  const catalogStoresLabel = document.getElementById("sb-catalog-stores-label");
  const catalogCountEl = document.getElementById("sb-catalog-count");
  const catalogList = document.getElementById("sb-catalog-list");
  const catalogMore = document.getElementById("sb-catalog-more");
  const catalogEmpty = document.getElementById("sb-catalog-empty");
  const browseCatalogBtn = document.getElementById("sb-browse-catalog");
  let catalogView = false;
  let catalogItems = [];
  let catalogLoaded = false;
  let catalogLoadPromise = null;
  let catalogCategory = "";
  let catalogQuery = "";
  let catalogVisibleCount = 40;
  let catalogExpandedId = "";
  const CATALOG_PAGE_SIZE = 40;

  function getCatalogStores() {{
    return getSavingsStores();
  }}

  function syncCatalogStoresChip() {{
    if (!catalogStoresChip || !catalogStoresLabel) return;
    const stores = Array.from(selected.retailer);
    let label = "My stores";
    if (stores.length === 1) label = displayRetailerName(stores[0]);
    else if (stores.length > 1) label = stores.length + " stores";
    catalogStoresLabel.textContent = label;
    catalogStoresChip.classList.toggle("is-active", stores.length > 0);
  }}

  function loadCatalogJson() {{
    if (catalogLoaded) return Promise.resolve(catalogItems);
    if (catalogLoadPromise) return catalogLoadPromise;
    catalogLoadPromise = fetch("catalog.json", {{ cache: "no-store" }})
      .then(function(r) {{
        if (!r || !r.ok) throw new Error("catalog.json HTTP " + (r && r.status));
        return r.json();
      }})
      .then(function(data) {{
        const items = (data && Array.isArray(data.items)) ? data.items : (Array.isArray(data) ? data : []);
        catalogItems = items;
        catalogLoaded = true;
        return catalogItems;
      }})
      .catch(function(err) {{
        console.error("[SnackBuddy] catalog.json failed", err);
        catalogItems = [];
        catalogLoaded = true;
        return catalogItems;
      }});
    return catalogLoadPromise;
  }}

  function buildCatalogCategoryChips() {{
    if (!catalogChips) return;
    const present = {{}};
    catalogItems.forEach(function(it) {{
      const c = String(it.category || "").toLowerCase().trim();
      if (c) present[c] = true;
    }});
    const ordered = CATEGORY_PREFERRED.filter(function(c) {{ return present[c]; }});
    Object.keys(present).forEach(function(c) {{
      if (ordered.indexOf(c) === -1) ordered.push(c);
    }});
    catalogChips.innerHTML = "";
    [{{ value: "", label: "All" }}].concat(ordered.map(function(v) {{
      return {{ value: v, label: categoryChipLabel(v) }};
    }})).forEach(function(opt) {{
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "sb-chip sb-cat-chip";
      btn.dataset.category = opt.value;
      btn.textContent = opt.label;
      btn.setAttribute("aria-pressed", opt.value === catalogCategory ? "true" : "false");
      if (opt.value === catalogCategory) btn.classList.add("is-active");
      btn.addEventListener("click", function() {{
        catalogCategory = opt.value || "";
        catalogVisibleCount = CATALOG_PAGE_SIZE;
        catalogExpandedId = "";
        buildCatalogCategoryChips();
        renderCatalogList();
      }});
      catalogChips.appendChild(btn);
    }});
  }}

  function catalogPriceRange(it, stores) {{
    const prices = it.prices || {{}};
    const vals = [];
    stores.forEach(function(s) {{
      const p = prices[s];
      if (typeof p === "number" && p > 0) vals.push(p);
    }});
    if (!vals.length) return "";
    const lo = Math.min.apply(null, vals);
    const hi = Math.max.apply(null, vals);
    const fmt = function(n) {{ return "$" + (Math.round(n * 100) / 100).toFixed(2); }};
    if (Math.abs(lo - hi) < 0.005) return fmt(lo);
    return fmt(lo) + "–" + fmt(hi);
  }}

  function catalogCarriedStores(it, stores) {{
    const set = new Set((it.stores || []).map(normStoreKey));
    return stores.filter(function(s) {{ return set.has(normStoreKey(s)); }});
  }}

  function catalogDisplayName(it) {{
    const brand = String(it.brand || "").trim();
    const product = String(it.product_name || "").trim();
    if (!brand) return product;
    if (!product) return brand;
    const b = brand.toLowerCase();
    const p = product.toLowerCase();
    if (p === b || p.indexOf(b + " ") === 0 || p.indexOf(b) === 0) return product;
    return (brand + " " + product).trim();
  }}

  function catalogOnSaleAtStores(it, stores) {{
    const sale = new Set((it.on_sale_stores || []).map(normStoreKey));
    return stores.some(function(s) {{ return sale.has(normStoreKey(s)); }});
  }}

  function catalogMatchesQuery(it, q) {{
    if (!q) return true;
    const hay = [
      it.brand || "",
      it.product_name || "",
      catalogDisplayName(it),
      it.item_id || "",
      (it.flavors || []).join(" ")
    ].join(" ").toLowerCase();
    return hay.indexOf(q) !== -1;
  }}

  function filteredCatalogItems() {{
    const stores = getCatalogStores();
    const q = String(catalogQuery || "").trim().toLowerCase();
    let rows = catalogItems.filter(function(it) {{
      if (catalogCategory && String(it.category || "").toLowerCase() !== catalogCategory) return false;
      // Hide items not carried at any of the visitor's stores (BG defaults if none saved).
      if (!catalogCarriedStores(it, stores).length) return false;
      if (!catalogMatchesQuery(it, q)) return false;
      return true;
    }});
    rows = rows.slice().sort(function(a, b) {{
      const aSale = catalogOnSaleAtStores(a, stores) ? 1 : 0;
      const bSale = catalogOnSaleAtStores(b, stores) ? 1 : 0;
      if (aSale !== bSale) return bSale - aSale;
      const aOften = a.goes_on_sale_often ? 1 : 0;
      const bOften = b.goes_on_sale_often ? 1 : 0;
      if (aOften !== bOften) return bOften - aOften;
      const an = catalogDisplayName(a).toLowerCase();
      const bn = catalogDisplayName(b).toLowerCase();
      if (an < bn) return -1;
      if (an > bn) return 1;
      return (a.pack_size || 0) - (b.pack_size || 0);
    }});
    return rows;
  }}

  function catalogRowHtml(it, stores) {{
    const carried = catalogCarriedStores(it, stores);
    const onSale = catalogOnSaleAtStores(it, stores);
    const pack = Number(it.pack_size) || 0;
    const packBit = pack > 1 ? (pack + "-pack") : "";
    const flavorN = (it.flavors || []).length;
    const flavorBit = flavorN === 1 ? "1 flavor" : (flavorN + " flavors");
    const storeBit = carried.length
      ? carried.map(displayRetailerName).join(", ")
      : ((it.stores || []).map(displayRetailerName).join(", ") || "—");
    const metaParts = [];
    if (packBit) metaParts.push(packBit);
    metaParts.push(flavorBit + " · " + storeBit);
    const price = catalogPriceRange(it, stores.length ? stores : (it.stores || []));
    const badges = [];
    if (onSale) badges.push('<span class="sb-catalog-badge is-sale">On sale now</span>');
    if (it.goes_on_sale_often) badges.push('<span class="sb-catalog-badge is-often">Goes on sale often</span>');
    if (it.is_new) badges.push('<span class="sb-catalog-badge is-new">New</span>');
    const img = it.image_url
      ? ('<img class="sb-catalog-thumb" src="' + String(it.image_url).replace(/"/g, "&quot;") + '" alt="" loading="lazy" />')
      : '<div class="sb-catalog-thumb-ph" aria-hidden="true"></div>';
    const name = catalogDisplayName(it);
    const starred = starredIds.has(it.item_id);
    const open = catalogExpandedId === it.item_id;
    let expand = "";
    if (open) {{
      const flavorSale = it.on_sale_flavors || {{}};
      const flavors = (it.flavors || []).slice();
      if (!flavors.length) flavors.push("Standard");
      // Ensure on-sale flavors appear even if missing from Description/Flavor list.
      Object.keys(flavorSale).forEach(function(f) {{
        if (flavors.indexOf(f) === -1) flavors.push(f);
      }});
      expand = '<div class="sb-catalog-expand">' + flavors.map(function(f) {{
        const rawStores = flavorSale[f] || [];
        const visStores = rawStores.filter(function(s) {{
          return stores.indexOf(normStoreKey(s)) !== -1 || !stores.length;
        }});
        // Fall back: item-level sale stores when flavor map is empty but item is on sale.
        const useStores = visStores.length ? visStores : (onSale && !Object.keys(flavorSale).length ? (it.on_sale_stores || []) : []);
        const saleHere = useStores.length > 0;
        const saleNote = saleHere
          ? (" · on sale at " + useStores.map(displayRetailerName).join(", "))
          : "";
        return '<div class="sb-catalog-flavor' + (saleHere ? " is-sale" : "") + '">' +
          escapeHtml(f) + saleNote + "</div>";
      }}).join("") + "</div>";
    }}
    const starLabel = starred ? "Remove from My Items" : "Save to My Items";
    return (
      '<article class="sb-catalog-row' + (open ? " is-open" : "") + '" data-item-id="' + escapeHtml(it.item_id) + '" role="button" tabindex="0">' +
        '<button type="button" class="sb-star-btn' + (starred ? " is-starred" : "") + '" aria-pressed="' + (starred ? "true" : "false") + '" aria-label="' + starLabel + '" title="' + starLabel + '">' +
          '<svg class="sb-star-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M12 3.6l2.47 5.01 5.53.8-4 3.9.94 5.5L12 16.9l-4.94 2.6.94-5.5-4-3.9 5.53-.8L12 3.6z"/></svg>' +
        "</button>" +
        img +
        '<div class="sb-catalog-main">' +
          '<p class="sb-catalog-name">' + escapeHtml(name) + "</p>" +
          '<p class="sb-catalog-meta">' + escapeHtml(metaParts.join(" · ")) + "</p>" +
          (price ? ('<p class="sb-catalog-price">' + escapeHtml(price) + "</p>") : "") +
          (badges.length ? ('<div class="sb-catalog-badges">' + badges.join("") + "</div>") : "") +
          (open ? expand : "") +
        "</div>" +
      "</article>"
    );
  }}

  function escapeHtml(s) {{
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }}

  function catalogStoreProductCount() {{
    const stores = getCatalogStores();
    return catalogItems.filter(function(it) {{
      return catalogCarriedStores(it, stores).length > 0;
    }}).length;
  }}

  function syncCatalogSearchPlaceholder() {{
    if (!catalogSearch) return;
    const n = catalogStoreProductCount();
    const nFmt = Number(n).toLocaleString("en-US");
    catalogSearch.placeholder = "Search " + nFmt + " products — Quest, Celsius, protein bars…";
  }}

  function renderCatalogList() {{
    if (!catalogList) return;
    const stores = getCatalogStores();
    const rows = filteredCatalogItems();
    const shown = rows.slice(0, catalogVisibleCount);
    catalogList.innerHTML = shown.map(function(it) {{ return catalogRowHtml(it, stores); }}).join("");
    if (catalogMore) {{
      catalogMore.hidden = shown.length >= rows.length;
      catalogMore.textContent = "Show more (" + Math.min(CATALOG_PAGE_SIZE, Math.max(0, rows.length - shown.length)) + ")";
    }}
    if (catalogCountEl) {{
      catalogCountEl.textContent = rows.length === 1 ? "1 product" : (rows.length + " products");
    }}
    syncCatalogSearchPlaceholder();
    if (catalogEmpty) {{
      if (!rows.length) {{
        const q = String(catalogQuery || "").trim();
        const safeQ = escapeHtml(q || "that");
        catalogEmpty.hidden = false;
        catalogEmpty.innerHTML = "No products match '" + safeQ + "'. Want us to track it? " +
          '<a href="mailto:savewithsnackbuddy@gmail.com?subject=' + encodeURIComponent("Please track " + (q || "a snack")) + '">Email us</a>';
      }} else {{
        catalogEmpty.hidden = true;
        catalogEmpty.innerHTML = "";
      }}
    }}
    syncStarButtons();
  }}

  function setCatalogUrl(on) {{
    try {{
      const url = new URL(window.location.href);
      if (on) url.searchParams.set("view", "catalog");
      else url.searchParams.delete("view");
      const next = url.pathname + (url.searchParams.toString() ? "?" + url.searchParams.toString() : "") + url.hash;
      window.history.replaceState({{}}, "", next);
    }} catch (err) {{}}
  }}

  function syncHeaderHeightVar() {{
    const header = document.querySelector(".sb-header");
    let h = 72;
    if (header) {{
      const rect = header.getBoundingClientRect();
      if (rect && rect.height) h = Math.ceil(rect.height);
    }}
    document.documentElement.style.setProperty("--sb-header-h", h + "px");
  }}
  syncHeaderHeightVar();
  window.addEventListener("resize", syncHeaderHeightVar);

  function openCatalogView(opts) {{
    opts = opts || {{}};
    catalogView = true;
    document.body.classList.add("sb-catalog-open");
    if (catalogEl) catalogEl.hidden = false;
    setCatalogUrl(true);
    closeNav && closeNav();
    syncHeaderHeightVar();
    // Jump to top of catalog so the sticky back/search bar starts in view.
    try {{ window.scrollTo(0, 0); }} catch (err) {{}}
    loadCatalogJson().then(function() {{
      buildCatalogCategoryChips();
      syncCatalogStoresChip();
      renderCatalogList();
      syncHeaderHeightVar();
      try {{ window.scrollTo(0, 0); }} catch (err) {{}}
      if (opts.focusSearch && catalogSearch) {{
        window.setTimeout(function() {{
          try {{ catalogSearch.focus(); }} catch (err) {{}}
        }}, 50);
      }}
    }});
    trackEvent("catalog_open", {{}});
  }}

  function closeCatalogView() {{
    catalogView = false;
    document.body.classList.remove("sb-catalog-open");
    if (catalogEl) catalogEl.hidden = true;
    setCatalogUrl(false);
    syncFloatButton();
  }}

  // Expose for stores popover / filter changes while catalog is open.
  window.__sbRefreshCatalog = function() {{
    if (!catalogView) return;
    syncCatalogStoresChip();
    renderCatalogList();
  }};

  searchToggle?.addEventListener("click", function() {{
    openCatalogView({{ focusSearch: true }});
  }});
  browseCatalogBtn?.addEventListener("click", function() {{
    openCatalogView({{ focusSearch: false }});
  }});
  catalogBack?.addEventListener("click", function() {{
    closeCatalogView();
  }});
  catalogMore?.addEventListener("click", function() {{
    catalogVisibleCount += CATALOG_PAGE_SIZE;
    renderCatalogList();
  }});
  catalogStoresChip?.addEventListener("click", function(ev) {{
    ev.preventDefault();
    // Reuse the main stores popover / chip handler if available.
    if (storesChip) storesChip.click();
  }});
  let catalogSearchTimer = null;
  catalogSearch?.addEventListener("input", function() {{
    clearTimeout(catalogSearchTimer);
    catalogSearchTimer = setTimeout(function() {{
      catalogQuery = String(catalogSearch.value || "");
      catalogVisibleCount = CATALOG_PAGE_SIZE;
      catalogExpandedId = "";
      renderCatalogList();
    }}, 120);
  }});
  catalogList?.addEventListener("click", function(ev) {{
    const t = ev.target;
    if (!t || !t.closest) return;
    if (t.closest(".sb-star-btn")) {{
      ev.preventDefault();
      ev.stopPropagation();
      const row = t.closest(".sb-catalog-row");
      if (row) {{
        const nameEl = row.querySelector(".sb-catalog-name");
        toggleStar(
          row.dataset.itemId || "",
          (nameEl && nameEl.textContent && nameEl.textContent.trim()) || ""
        );
      }}
      return;
    }}
    const row = t.closest(".sb-catalog-row");
    if (!row || !catalogList.contains(row)) return;
    const id = row.dataset.itemId || "";
    catalogExpandedId = (catalogExpandedId === id) ? "" : id;
    renderCatalogList();
  }});
  document.querySelectorAll('a.sb-nav-link[data-view="catalog"]').forEach(function(a) {{
    a.addEventListener("click", function(ev) {{
      ev.preventDefault();
      openCatalogView({{ focusSearch: false }});
    }});
  }});
  document.querySelectorAll('a.sb-nav-link[data-view="deals"]').forEach(function(a) {{
    a.addEventListener("click", function() {{
      if (catalogView) closeCatalogView();
    }});
  }});

  if (searchClear) {{
    searchClear.addEventListener("click", closeSearch);
  }}
  if (searchInput) {{
    searchInput.addEventListener("blur", function() {{
      window.setTimeout(function() {{
        if (searchWrap && searchWrap.contains(document.activeElement)) return;
        if (!String(searchInput.value || "").trim()) setSearchExpanded(false, false);
      }}, 150);
    }});
    searchInput.addEventListener("keydown", function(ev) {{
      if (ev.key === "Escape") closeSearch();
    }});
  }}
  if (sortSelect) {{
    sortSelect.addEventListener("change", function() {{
      updateBadges();
      applyFiltersAndSort();
    }});
  }}

  // Legacy hook (sort menu moved into the Filters sheet).
  function closeSortMenu() {{}}

  buildCategoryChips();
  buildFilterSheet();
  updateBadges();
  updateChipRowScroll();
  syncStarButtons();
  applyFiltersAndSort();

  try {{
    const bootParams = new URLSearchParams(window.location.search || "");
    if (bootParams.get("view") === "catalog") openCatalogView({{ focusSearch: true }});
  }} catch (err) {{}}

  fetch("last_sale.json", {{ cache: "no-store" }})
    .then(function(r) {{ return r.ok ? r.json() : null; }})
    .then(function(data) {{
      if (data && data.items) lastSaleItems = data.items;
      if (myItemsView) applyFiltersAndSort();
      else renderSavingsPanel();
    }})
    .catch(function() {{}});

  fetch("savings.json", {{ cache: "no-store" }})
    .then(function(r) {{
      if (!r || !r.ok) throw new Error("savings.json HTTP " + (r && r.status));
      return r.json();
    }})
    .then(function(data) {{
      if (!data || typeof data !== "object" || Array.isArray(data)) {{
        throw new Error("savings.json invalid payload");
      }}
      savingsByItem = data;
      savingsLoadStatus = "ok";
      if (myItemsView) applyFiltersAndSort();
      else renderSavingsPanel();
    }})
    .catch(function(err) {{
      console.error("[SnackBuddy] savings.json failed to load", err);
      savingsByItem = {{}};
      savingsLoadStatus = "error";
      if (myItemsView) applyFiltersAndSort();
      else renderSavingsPanel();
    }});

  /* ============================
     WELCOME PREFS + SUPABASE AUTH
     ============================ */
  const WELCOME_STORAGE_KEY = "sb_welcome_v3";
  const RETAILER_ORDER = ["walmart", "target", "kroger", "harris teeter", "meijer"];

  function countMatchingCards() {{
    const q = (searchInput?.value || "").trim().toLowerCase();
    let n = 0;
    cards.forEach(function(card) {{
      const r = card.dataset.retailer;
      const s = card.dataset.section;
      const c = card.dataset.category;
      const b = card.dataset.brand;
      const hay = (card.dataset.search || "").toLowerCase();
      const matchR = cardMatchesActiveStores(r);
      const matchS = selected.section.size === 0 || selected.section.has(s);
      const matchC = selected.category.size === 0 || selected.category.has(c);
      const matchB = selected.brand.size === 0 || selected.brand.has(b);
      const matchQ = !q || hay.indexOf(q) !== -1;
      const matchT = cardMatchesTier(card);
      if (matchR && matchS && matchC && matchB && matchQ && matchT) n += 1;
    }});
    return n;
  }}

  function getPreferredStoreList() {{
    const prefs = loadWelcomePrefs();
    if (prefs && Array.isArray(prefs.retailers) && prefs.retailers.length) {{
      return prefs.retailers.map(function(v) {{ return String(v || "").trim(); }}).filter(Boolean);
    }}
    return Array.from(selected.retailer);
  }}

  function hasSavedStores() {{
    return getPreferredStoreList().length > 0;
  }}

  function countOnSaleItemsAtStores() {{
    // Individual flavors (data-deal-count), same units as the hero "N are on sale today".
    // Uses saved stores when set; otherwise BG defaults (W/T/K/Meijer — no Harris Teeter).
    let n = 0;
    cards.forEach(function(card) {{
      if (card.dataset.onSale === "0" || card.classList.contains("sb-my-items-offrow")) return;
      if (!cardMatchesActiveStores(card.dataset.retailer)) return;
      const dc = parseInt(card.dataset.dealCount || "1", 10);
      n += (Number.isFinite(dc) && dc > 0) ? dc : 1;
    }});
    return n;
  }}

  function formatOnSaleCountLine() {{
    const n = countOnSaleItemsAtStores();
    const place = hasSavedStores() ? "at your stores" : "in Bowling Green";
    return n + " on sale " + place;
  }}

  function preferredStoreRank(retailer) {{
    const preferred = getPreferredStoreList();
    if (!preferred.length) return 0;
    const key = String(retailer || "").trim();
    const idx = preferred.indexOf(key);
    return idx === -1 ? preferred.length + 1 : idx;
  }}

  let openStoresPicker = function() {{}};

  function updateStoresLine() {{
    const line = document.getElementById("sb-stores-line");
    const names = document.getElementById("sb-stores-line-names");
    if (!line || !names) return;
    const stores = getPreferredStoreList();
    if (!stores.length) {{
      line.hidden = true;
      names.textContent = "";
      return;
    }}
    names.textContent = stores.map(displayRetailerName).join(", ");
    line.hidden = false;
  }}

  function applyWelcomePrefs(prefs) {{
    selected.retailer.clear();
    selected.section.clear();
    selected.category.clear();
    selected.brand.clear();
    (prefs.retailers || []).forEach(function(v) {{ if (v) selected.retailer.add(v); }});
    // Main chip row is single-select: seed a category only if exactly one was saved, else All.
    if (Array.isArray(prefs.categories) && prefs.categories.length === 1 && prefs.categories[0]) {{
      selected.category.add(prefs.categories[0]);
    }}
    syncAllCheckboxes();
    updateBadges();
    applyFiltersAndSort();
    refreshFilterOptionLists();
    updateStoresLine();
  }}

  function saveWelcomePrefs(prefs) {{
    try {{
      localStorage.setItem(WELCOME_STORAGE_KEY, JSON.stringify(prefs));
    }} catch (err) {{}}
  }}

  function loadWelcomePrefs() {{
    try {{
      const raw = localStorage.getItem(WELCOME_STORAGE_KEY);
      if (!raw) return null;
      return JSON.parse(raw);
    }} catch (err) {{
      return null;
    }}
  }}

  function isValidEmail(value) {{
    return /^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$/.test(String(value || "").trim());
  }}

  function submitMagicLinkSignup(email) {{
    return requestMagicLink(email);
  }}

  function wantsPrefsPanel() {{
    try {{
      const params = new URLSearchParams(window.location.search || "");
      return params.get("prefs") === "1" || params.get("edit") === "stores";
    }} catch (err) {{
      return false;
    }}
  }}

  function clearPrefsParams() {{
    try {{
      const url = new URL(window.location.href);
      let changed = false;
      if (url.searchParams.has("prefs")) {{
        url.searchParams.delete("prefs");
        changed = true;
      }}
      if (url.searchParams.has("edit")) {{
        url.searchParams.delete("edit");
        changed = true;
      }}
      if (!changed) return;
      const next = url.pathname + (url.searchParams.toString() ? "?" + url.searchParams.toString() : "") + url.hash;
      window.history.replaceState({{}}, "", next);
    }} catch (err) {{}}
  }}

  let syncWelcomeEditHint = function() {{}};

  function initWelcomeFlow() {{
    const root = document.getElementById("sb-welcome");
    if (!root) return;

    const existing = loadWelcomePrefs();

    const retailerBox = document.getElementById("sb-welcome-retailers");
    const categoryBox = document.getElementById("sb-welcome-categories");
    const otherWrap = document.getElementById("sb-welcome-other-wrap");
    const otherInput = document.getElementById("sb-welcome-other-store");
    const previewStores = document.getElementById("sb-welcome-preview-stores");
    const previewCategories = document.getElementById("sb-welcome-preview-categories");
    const welcomeSignup = document.getElementById("sb-welcome-signup");
    const welcomeEmail = document.getElementById("sb-welcome-email");
    const welcomeEmailSubmit = document.getElementById("sb-welcome-email-submit");
    const welcomeEmailStatus = document.getElementById("sb-welcome-email-status");
    const dots = Array.from(root.querySelectorAll(".sb-welcome-dot"));
    const steps = Array.from(root.querySelectorAll(".sb-welcome-step"));

    const draft = {{
      retailers: new Set(),
      categories: new Set(),
      otherOn: false,
      otherStore: "",
      email: "",
      subscribed: false,
    }};

    let step = 0;

    function getOtherStore() {{
      if (!draft.otherOn) return "";
      const typed = otherInput ? String(otherInput.value || "").trim() : draft.otherStore;
      draft.otherStore = typed;
      return typed;
    }}

    function availableRetailers() {{
      const present = uniq(cards.map(function(c) {{ return c.dataset.retailer; }}));
      const ordered = RETAILER_ORDER.filter(function(r) {{ return present.indexOf(r) !== -1; }});
      present.forEach(function(r) {{
        if (ordered.indexOf(r) === -1) ordered.push(r);
      }});
      return ordered;
    }}

    function setEmailStatus(msg, isError) {{
      if (!welcomeEmailStatus) return;
      welcomeEmailStatus.textContent = msg || "";
      welcomeEmailStatus.classList.toggle("is-error", !!isError);
    }}

    function wireWelcomeSignup() {{
      if (!welcomeSignup || welcomeSignup.dataset.wired === "1") return;
      welcomeSignup.dataset.wired = "1";
      welcomeSignup.addEventListener("submit", function(ev) {{
        ev.preventDefault();
        if (sbAuthUser) {{
          if (welcomeEmailSubmit) welcomeEmailSubmit.disabled = true;
          setEmailStatus("Re-enabling emails…");
          resubscribeSignedInUser().then(function(ok) {{
            setEmailStatus(ok
              ? "You’re back on the list — emails re-enabled."
              : "Couldn’t update preferences. Try again.", !ok);
            if (!ok && welcomeEmailSubmit) welcomeEmailSubmit.disabled = false;
          }});
          return;
        }}
        if (draft.subscribed) {{
          setEmailStatus(MAGIC_LINK_MSG);
          return;
        }}
        const email = welcomeEmail ? String(welcomeEmail.value || "").trim() : "";
        if (!isValidEmail(email)) {{
          setEmailStatus("Enter a valid email address.", true);
          if (welcomeEmail) welcomeEmail.focus();
          return;
        }}
        if (!sbClient) {{
          setEmailStatus("Signup is temporarily unavailable. Try again later.", true);
          return;
        }}
        if (welcomeEmailSubmit) welcomeEmailSubmit.disabled = true;
        setEmailStatus("Sending link…");
        draft.email = email;
        submitMagicLinkSignup(email).then(function() {{
          draft.subscribed = true;
          markEmailSubscribed();
          syncMyItemsNotifyVisibility();
          trackEvent("generate_lead", {{ method: "welcome_flow" }});
          setEmailStatus(MAGIC_LINK_MSG);
          if (welcomeEmail) welcomeEmail.blur();
        }}).catch(function(err) {{
          console.error("[SnackBuddy] welcome magic link failed", err);
          setEmailStatus("Couldn’t send the link. Try again or use the footer form.", true);
          if (welcomeEmailSubmit) welcomeEmailSubmit.disabled = false;
        }});
      }});
    }}

    function availableCategories() {{
      const present = uniq(cards.map(function(c) {{ return c.dataset.category; }}));
      const preferred = [
        "bars",
        "chips & crunchy",
        "cookies & sweets",
        "meat snack",
        "wraps",
        "energy drink",
        "rtd protein shake",
        "protein powder",
      ];
      const ordered = preferred.filter(function(c) {{ return present.indexOf(c) !== -1; }});
      present.forEach(function(c) {{
        if (ordered.indexOf(c) === -1) ordered.push(c);
      }});
      return ordered;
    }}

    function syncOtherUi() {{
      if (otherWrap) otherWrap.hidden = !draft.otherOn;
      if (draft.otherOn && otherInput) {{
        window.setTimeout(function() {{ otherInput.focus(); }}, 50);
      }}
    }}

    function welcomeCategoryLabel(val) {{
      const map = {{
        "bars": "Protein Bars",
        "chips & crunchy": "Chips & Crunchy",
        "cookies & sweets": "Cookies & Sweets",
        "meat snack": "Meat Stick",
        "wraps": "Wraps",
        "energy drink": "Energy Drink",
        "rtd protein shake": "RTD Shake",
        "protein powder": "Protein Powder",
      }};
      return map[String(val || "").toLowerCase()] || titleize(val);
    }}

    function welcomeRetailerIcon(val) {{
      const key = String(val || "").toLowerCase();
      const icons = {{
        walmart: '<img class="sb-welcome-logo" src="assets/walmart-logo.png" alt="" width="64" height="64" loading="lazy" />',
        target: '<svg viewBox="0 0 64 64" aria-hidden="true"><rect width="64" height="64" rx="14" fill="#CC0000"/><circle cx="32" cy="32" r="18" fill="#fff"/><circle cx="32" cy="32" r="11.5" fill="#CC0000"/><circle cx="32" cy="32" r="5" fill="#fff"/></svg>',
        kroger: '<img class="sb-welcome-logo" src="assets/kroger-logo.png" alt="" width="120" height="48" loading="lazy" />',
        "harris teeter": '<img class="sb-welcome-logo" src="assets/harris-teeter-logo.png" alt="" width="120" height="48" loading="lazy" />',
        meijer: '<img class="sb-welcome-logo" src="assets/meijer-logo.png" alt="" width="120" height="48" loading="lazy" />',
        other: '<svg viewBox="0 0 64 64" aria-hidden="true"><rect width="64" height="64" rx="14" fill="#F1F5F9" stroke="#94A3B8" stroke-width="2"/><path d="M32 18v28M18 32h28" stroke="#475569" stroke-width="4" stroke-linecap="round"/></svg>',
      }};
      return icons[key] || icons.other;
    }}

    function welcomeRetailerLabel(val) {{
      const key = String(val || "").toLowerCase();
      const map = {{
        walmart: "Walmart®",
        target: "Target®",
        kroger: "Kroger®",
        "harris teeter": "Harris Teeter · Baltimore",
        meijer: "Meijer®",
        other: "Other",
      }};
      return map[key] || (titleize(val) + "®");
    }}

    function welcomeCategoryIcon(val) {{
      const key = String(val || "").toLowerCase();
      const icons = {{
        bars: '<img class="sb-welcome-logo" src="assets/category-bars.png" alt="" width="64" height="64" loading="lazy" />',
        "chips & crunchy": '<img class="sb-welcome-logo" src="assets/category-chips.png" alt="" width="64" height="64" loading="lazy" />',
        "cookies & sweets": '<img class="sb-welcome-logo" src="assets/category-cookies.png" alt="" width="64" height="64" loading="lazy" />',
        "energy drink": '<img class="sb-welcome-logo" src="assets/category-energy.png" alt="" width="64" height="64" loading="lazy" />',
        "meat snack": '<img class="sb-welcome-logo" src="assets/category-meat.png" alt="" width="64" height="64" loading="lazy" />',
        wraps: '<img class="sb-welcome-logo" src="assets/category-wraps.png" alt="" width="64" height="64" loading="lazy" />',
        "rtd protein shake": '<img class="sb-welcome-logo" src="assets/category-rtd.png" alt="" width="64" height="64" loading="lazy" />',
        "protein powder": '<img class="sb-welcome-logo" src="assets/category-powder.png" alt="" width="64" height="64" loading="lazy" />',
      }};
      return icons[key] || '<svg viewBox="0 0 64 64" aria-hidden="true"><rect width="64" height="64" rx="14" fill="#F1F5F9"/><circle cx="32" cy="32" r="12" fill="#94A3B8"/></svg>';
    }}

    function makeWelcomeTile(opts) {{
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "sb-welcome-chip" + (opts.on ? " is-on" : "");
      btn.setAttribute("aria-pressed", opts.on ? "true" : "false");
      btn.setAttribute("aria-label", opts.label);
      const icon = document.createElement("span");
      const html = opts.iconHtml || "";
      icon.className = "sb-welcome-chip-icon";
      if (/<img\\b/i.test(html)) icon.classList.add("is-logo");
      if (/kroger-logo/i.test(html)) icon.classList.add("is-kroger");
      if (/meijer-logo/i.test(html)) icon.classList.add("is-meijer");
      if (/walmart-logo/i.test(html)) icon.classList.add("is-walmart");
      if (/category-/i.test(html)) icon.classList.add("is-category");
      icon.innerHTML = html;
      const label = document.createElement("span");
      label.className = "sb-welcome-chip-label";
      label.textContent = opts.label;
      btn.appendChild(icon);
      btn.appendChild(label);
      btn.addEventListener("click", function() {{
        const next = opts.onToggle();
        btn.classList.toggle("is-on", !!next);
        btn.setAttribute("aria-pressed", next ? "true" : "false");
        updateLivePreview();
      }});
      return btn;
    }}

    function renderRetailerChips() {{
      if (!retailerBox) return;
      retailerBox.innerHTML = "";
      availableRetailers().forEach(function(val) {{
        retailerBox.appendChild(makeWelcomeTile({{
          label: welcomeRetailerLabel(val),
          iconHtml: welcomeRetailerIcon(val),
          on: draft.retailers.has(val),
          onToggle: function() {{
            if (draft.retailers.has(val)) draft.retailers.delete(val);
            else draft.retailers.add(val);
            return draft.retailers.has(val);
          }},
        }}));
      }});
      retailerBox.appendChild(makeWelcomeTile({{
        label: welcomeRetailerLabel("other"),
        iconHtml: welcomeRetailerIcon("other"),
        on: draft.otherOn,
        onToggle: function() {{
          draft.otherOn = !draft.otherOn;
          if (!draft.otherOn) {{
            draft.otherStore = "";
            if (otherInput) otherInput.value = "";
          }}
          syncOtherUi();
          return draft.otherOn;
        }},
      }}));
      syncOtherUi();
    }}

    function renderChips(container, values, setRef) {{
      if (!container) return;
      container.innerHTML = "";
      values.forEach(function(val) {{
        container.appendChild(makeWelcomeTile({{
          label: welcomeCategoryLabel(val),
          iconHtml: welcomeCategoryIcon(val),
          on: setRef.has(val),
          onToggle: function() {{
            if (setRef.has(val)) setRef.delete(val);
            else setRef.add(val);
            return setRef.has(val);
          }},
        }}));
      }});
    }}

    function updateLivePreview() {{
      selected.retailer = new Set(draft.retailers);
      selected.section.clear();
      selected.category = new Set(draft.categories);
      selected.brand.clear();
      const n = countMatchingCards();
      syncAllCheckboxes();
      updateBadges();
      applyFiltersAndSort();
      const other = getOtherStore();
      if (previewStores) {{
        let msg = draft.retailers.size
          ? (n + " deals match your stores")
          : "Nothing selected = all stores (" + n + " deals)";
        if (draft.otherOn && other) msg += " · noted: " + other;
        else if (draft.otherOn) msg += " · add your store below";
        previewStores.textContent = msg;
      }}
      if (previewCategories) {{
        previewCategories.textContent = draft.categories.size
          ? (n + " deals in your pantry mix")
          : "Nothing selected = all categories (" + n + " deals)";
      }}
    }}

    function setStep(next) {{
      step = next;
      steps.forEach(function(el) {{
        el.classList.toggle("is-active", Number(el.dataset.step) === step);
      }});
      dots.forEach(function(dot) {{
        dot.classList.toggle("is-on", Number(dot.dataset.dot) <= step);
      }});
      if (step === 1) {{
        renderRetailerChips();
        if (otherInput && draft.otherStore) otherInput.value = draft.otherStore;
        updateLivePreview();
      }}
      if (step === 2) {{
        getOtherStore();
        renderChips(categoryBox, availableCategories(), draft.categories);
        updateLivePreview();
      }}
      if (step === 3) {{
        wireWelcomeSignup();
        // Welcome email step counts as ask #1 (once per welcome session).
        if (!welcomeEmailAskCounted) {{
          welcomeEmailAskCounted = true;
          recordEmailAsk();
        }}
        window.setTimeout(function() {{
          if (welcomeEmail) {{
            welcomeEmail.focus({{ preventScroll: false }});
            try {{
              welcomeEmail.scrollIntoView({{ behavior: "smooth", block: "center" }});
            }} catch (err) {{}}
          }}
        }}, 80);
      }}
    }}

    if (otherInput) {{
      otherInput.addEventListener("input", function() {{
        draft.otherStore = String(otherInput.value || "").trim();
        updateLivePreview();
      }});
    }}

    function showWelcomePanel() {{
      root.hidden = false;
      root.setAttribute("aria-hidden", "false");
      root.classList.add("open");
      document.body.classList.add("sb-welcome-lock");
      syncFloatButton();
    }}

    function hideWelcomePanel() {{
      root.classList.remove("open");
      document.body.classList.remove("sb-welcome-lock");
      syncFloatButton();
      window.setTimeout(function() {{
        root.hidden = true;
        root.setAttribute("aria-hidden", "true");
        syncFloatButton();
      }}, 320);
    }}

    function openWelcome() {{
      // First paint: show the full (unfiltered) deals page behind the frost
      draft.retailers.clear();
      draft.categories.clear();
      draft.otherOn = false;
      draft.otherStore = "";
      if (otherInput) otherInput.value = "";
      selected.retailer.clear();
      selected.section.clear();
      selected.category.clear();
      selected.brand.clear();
      syncAllCheckboxes();
      updateBadges();
      applyFiltersAndSort();
      refreshFilterOptionLists();

      showWelcomePanel();
      setStep(0);
    }}

    function closeWelcome(prefs) {{
      const payload = Object.assign({{ completed: true }}, prefs || {{}});
      payload.retailers = Array.from(draft.retailers);
      payload.categories = Array.from(draft.categories);
      payload.otherStore = getOtherStore();
      if (draft.email) payload.email = draft.email;
      if (draft.subscribed) payload.subscribed = true;
      saveWelcomePrefs(payload);
      applyWelcomePrefs(payload);
      hideWelcomePanel();
      showStoresSavedToast(payload.retailers);
      try {{
        const dealsList = document.getElementById("deals-list");
        if (dealsList) dealsList.scrollIntoView({{ behavior: "smooth", block: "start" }});
      }} catch (err) {{}}
    }}

    root.querySelectorAll("[data-welcome-next]").forEach(function(btn) {{
      btn.addEventListener("click", function() {{
        if (step < 3) setStep(step + 1);
        else closeWelcome({{}});
      }});
    }});
    root.querySelectorAll("[data-welcome-back]").forEach(function(btn) {{
      btn.addEventListener("click", function() {{
        if (step > 0) setStep(step - 1);
      }});
    }});
    root.querySelectorAll("[data-welcome-skip]").forEach(function(btn) {{
      btn.addEventListener("click", function() {{
        if (step === 0) {{
          draft.retailers.clear();
          draft.categories.clear();
          draft.otherOn = false;
          draft.otherStore = "";
          closeWelcome({{ skipped: true }});
        }} else {{
          closeWelcome({{}});
        }}
      }});
    }});
    const welcomeCloseBtn = document.getElementById("sb-welcome-close");
    if (welcomeCloseBtn) {{
      welcomeCloseBtn.addEventListener("click", function() {{
        if (step === 0) {{
          draft.retailers.clear();
          draft.categories.clear();
          draft.otherOn = false;
          draft.otherStore = "";
          closeWelcome({{ skipped: true }});
        }} else {{
          closeWelcome({{}});
        }}
      }});
    }}

    if (existing && existing.completed) {{
      applyWelcomePrefs(existing);
    }} else if (!wantsPrefsPanel()) {{
      openWelcome();
    }} else {{
      // Prefs URL wins over first-run welcome; still apply any saved prefs behind it.
      if (existing) applyWelcomePrefs(existing);
    }}
    updateStoresLine();
  }}

  function initPrefsPanel() {{
    const root = document.getElementById("sb-prefs");
    if (!root) return;

    const retailerBox = document.getElementById("sb-prefs-retailers");
    const categoryBox = document.getElementById("sb-prefs-categories");
    const otherWrap = document.getElementById("sb-prefs-other-wrap");
    const otherInput = document.getElementById("sb-prefs-other-store");
    const signedInBox = document.getElementById("sb-prefs-email-signed-in");
    const signedOutBox = document.getElementById("sb-prefs-email-signed-out");
    const weeklyToggle = document.getElementById("sb-prefs-weekly");
    const saveBtn = document.getElementById("sb-prefs-save");
    const signupForm = document.getElementById("sb-prefs-signup");
    const signupEmail = document.getElementById("sb-prefs-email");
    const signupSubmit = document.getElementById("sb-prefs-email-submit");
    const signupStatus = document.getElementById("sb-prefs-email-status");

    const draft = {{
      retailers: new Set(),
      categories: new Set(),
      otherOn: false,
      otherStore: "",
      weeklyEmail: true,
    }};
    let saveTimer = null;
    let open = false;

    function prefsCategoryLabel(val) {{
      const map = {{
        "bars": "Protein Bars",
        "chips & crunchy": "Chips & Crunchy",
        "cookies & sweets": "Cookies & Sweets",
        "meat snack": "Meat Stick",
        "wraps": "Wraps",
        "energy drink": "Energy Drink",
        "rtd protein shake": "RTD Shake",
        "protein powder": "Protein Powder",
      }};
      return map[String(val || "").toLowerCase()] || titleize(val);
    }}

    function prefsRetailerIcon(val) {{
      const key = String(val || "").toLowerCase();
      const icons = {{
        walmart: '<img class="sb-welcome-logo" src="assets/walmart-logo.png" alt="" width="64" height="64" loading="lazy" />',
        target: '<svg viewBox="0 0 64 64" aria-hidden="true"><rect width="64" height="64" rx="14" fill="#CC0000"/><circle cx="32" cy="32" r="18" fill="#fff"/><circle cx="32" cy="32" r="11.5" fill="#CC0000"/><circle cx="32" cy="32" r="5" fill="#fff"/></svg>',
        kroger: '<img class="sb-welcome-logo" src="assets/kroger-logo.png" alt="" width="120" height="48" loading="lazy" />',
        "harris teeter": '<img class="sb-welcome-logo" src="assets/harris-teeter-logo.png" alt="" width="120" height="48" loading="lazy" />',
        meijer: '<img class="sb-welcome-logo" src="assets/meijer-logo.png" alt="" width="120" height="48" loading="lazy" />',
        other: '<svg viewBox="0 0 64 64" aria-hidden="true"><rect width="64" height="64" rx="14" fill="#F1F5F9" stroke="#94A3B8" stroke-width="2"/><path d="M32 18v28M18 32h28" stroke="#475569" stroke-width="4" stroke-linecap="round"/></svg>',
      }};
      return icons[key] || icons.other;
    }}

    function prefsRetailerLabel(val) {{
      const key = String(val || "").toLowerCase();
      const map = {{
        walmart: "Walmart®",
        target: "Target®",
        kroger: "Kroger®",
        "harris teeter": "Harris Teeter · Baltimore",
        meijer: "Meijer®",
        other: "Other",
      }};
      return map[key] || (titleize(val) + "®");
    }}

    function prefsCategoryIcon(val) {{
      const key = String(val || "").toLowerCase();
      const icons = {{
        bars: '<img class="sb-welcome-logo" src="assets/category-bars.png" alt="" width="64" height="64" loading="lazy" />',
        "chips & crunchy": '<img class="sb-welcome-logo" src="assets/category-chips.png" alt="" width="64" height="64" loading="lazy" />',
        "cookies & sweets": '<img class="sb-welcome-logo" src="assets/category-cookies.png" alt="" width="64" height="64" loading="lazy" />',
        "energy drink": '<img class="sb-welcome-logo" src="assets/category-energy.png" alt="" width="64" height="64" loading="lazy" />',
        "meat snack": '<img class="sb-welcome-logo" src="assets/category-meat.png" alt="" width="64" height="64" loading="lazy" />',
        wraps: '<img class="sb-welcome-logo" src="assets/category-wraps.png" alt="" width="64" height="64" loading="lazy" />',
        "rtd protein shake": '<img class="sb-welcome-logo" src="assets/category-rtd.png" alt="" width="64" height="64" loading="lazy" />',
        "protein powder": '<img class="sb-welcome-logo" src="assets/category-powder.png" alt="" width="64" height="64" loading="lazy" />',
      }};
      return icons[key] || '<svg viewBox="0 0 64 64" aria-hidden="true"><rect width="64" height="64" rx="14" fill="#F1F5F9"/><circle cx="32" cy="32" r="12" fill="#94A3B8"/></svg>';
    }}

    function makePrefsTile(opts) {{
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "sb-welcome-chip" + (opts.on ? " is-on" : "");
      btn.setAttribute("aria-pressed", opts.on ? "true" : "false");
      btn.setAttribute("aria-label", opts.label);
      const icon = document.createElement("span");
      const html = opts.iconHtml || "";
      icon.className = "sb-welcome-chip-icon";
      if (/<img\\b/i.test(html)) icon.classList.add("is-logo");
      if (/kroger-logo/i.test(html)) icon.classList.add("is-kroger");
      if (/meijer-logo/i.test(html)) icon.classList.add("is-meijer");
      if (/walmart-logo/i.test(html)) icon.classList.add("is-walmart");
      if (/category-/i.test(html)) icon.classList.add("is-category");
      icon.innerHTML = html;
      const label = document.createElement("span");
      label.className = "sb-welcome-chip-label";
      label.textContent = opts.label;
      btn.appendChild(icon);
      btn.appendChild(label);
      btn.addEventListener("click", function() {{
        const next = opts.onToggle();
        btn.classList.toggle("is-on", !!next);
        btn.setAttribute("aria-pressed", next ? "true" : "false");
      }});
      return btn;
    }}

    function availableRetailers() {{
      const present = uniq(cards.map(function(c) {{ return c.dataset.retailer; }}));
      const ordered = RETAILER_ORDER.filter(function(r) {{ return present.indexOf(r) !== -1; }});
      present.forEach(function(r) {{
        if (ordered.indexOf(r) === -1) ordered.push(r);
      }});
      return ordered;
    }}

    function availableCategories() {{
      const present = uniq(cards.map(function(c) {{ return c.dataset.category; }}));
      const preferred = [
        "bars",
        "chips & crunchy",
        "cookies & sweets",
        "meat snack",
        "wraps",
        "energy drink",
        "rtd protein shake",
        "protein powder",
      ];
      const ordered = preferred.filter(function(c) {{ return present.indexOf(c) !== -1; }});
      present.forEach(function(c) {{
        if (ordered.indexOf(c) === -1) ordered.push(c);
      }});
      return ordered;
    }}

    function syncOtherUi() {{
      if (otherWrap) otherWrap.hidden = !draft.otherOn;
    }}

    function renderRetailerChips() {{
      if (!retailerBox) return;
      retailerBox.innerHTML = "";
      availableRetailers().forEach(function(val) {{
        retailerBox.appendChild(makePrefsTile({{
          label: prefsRetailerLabel(val),
          iconHtml: prefsRetailerIcon(val),
          on: draft.retailers.has(val),
          onToggle: function() {{
            if (draft.retailers.has(val)) draft.retailers.delete(val);
            else draft.retailers.add(val);
            return draft.retailers.has(val);
          }},
        }}));
      }});
      retailerBox.appendChild(makePrefsTile({{
        label: prefsRetailerLabel("other"),
        iconHtml: prefsRetailerIcon("other"),
        on: draft.otherOn,
        onToggle: function() {{
          draft.otherOn = !draft.otherOn;
          if (!draft.otherOn) {{
            draft.otherStore = "";
            if (otherInput) otherInput.value = "";
          }}
          syncOtherUi();
          return draft.otherOn;
        }},
      }}));
      syncOtherUi();
    }}

    function renderCategoryChips() {{
      if (!categoryBox) return;
      categoryBox.innerHTML = "";
      availableCategories().forEach(function(val) {{
        categoryBox.appendChild(makePrefsTile({{
          label: prefsCategoryLabel(val),
          iconHtml: prefsCategoryIcon(val),
          on: draft.categories.has(val),
          onToggle: function() {{
            if (draft.categories.has(val)) draft.categories.delete(val);
            else draft.categories.add(val);
            return draft.categories.has(val);
          }},
        }}));
      }});
    }}

    function fillDraftFromPrefs(prefs) {{
      const src = prefs || {{}};
      draft.retailers = new Set(
        (Array.isArray(src.retailers) ? src.retailers : [])
          .map(function(v) {{ return String(v || "").trim(); }})
          .filter(Boolean)
      );
      draft.categories = new Set(
        (Array.isArray(src.categories) ? src.categories : [])
          .map(function(v) {{ return String(v || "").trim(); }})
          .filter(Boolean)
      );
      draft.otherStore = String(src.otherStore || "").trim();
      draft.otherOn = !!draft.otherStore;
      if (otherInput) otherInput.value = draft.otherStore;
    }}

    function syncEmailSection() {{
      if (signedInBox) signedInBox.hidden = !sbAuthUser;
      if (signedOutBox) signedOutBox.hidden = !!sbAuthUser;
      if (weeklyToggle && !sbAuthUser) weeklyToggle.checked = true;
    }}

    function loadWeeklyToggleFromServer() {{
      if (!sbClient || !sbAuthUser || !weeklyToggle) {{
        if (weeklyToggle) weeklyToggle.checked = !!draft.weeklyEmail;
        return Promise.resolve();
      }}
      return sbClient
        .from("subscribers")
        .select("weekly_email,deal_alerts,unsubscribed_at")
        .eq("user_id", sbAuthUser.id)
        .maybeSingle()
        .then(function(res) {{
          if (res && res.error) throw res.error;
          const row = res.data || null;
          if (!row) {{
            draft.weeklyEmail = true;
            weeklyToggle.checked = true;
            return;
          }}
          const optedIn = !!row.weekly_email && row.unsubscribed_at == null;
          draft.weeklyEmail = optedIn;
          weeklyToggle.checked = optedIn;
        }})
        .catch(function(err) {{
          console.error("[SnackBuddy] prefs email load failed", err);
          weeklyToggle.checked = !!draft.weeklyEmail;
        }});
    }}

    function showPanel() {{
      root.hidden = false;
      root.setAttribute("aria-hidden", "false");
      root.classList.add("open");
      document.body.classList.add("sb-welcome-lock");
      open = true;
      syncFloatButton();
    }}

    function hidePanel() {{
      root.classList.remove("open");
      document.body.classList.remove("sb-welcome-lock");
      open = false;
      syncFloatButton();
      window.setTimeout(function() {{
        if (!open) {{
          root.hidden = true;
          root.setAttribute("aria-hidden", "true");
          syncFloatButton();
        }}
      }}, 320);
    }}

    function openPrefsPanel() {{
      fillDraftFromPrefs(loadWelcomePrefs() || {{}});
      renderRetailerChips();
      renderCategoryChips();
      syncEmailSection();
      if (saveBtn) {{
        saveBtn.disabled = false;
        saveBtn.textContent = "Save preferences";
      }}
      showPanel();
      loadWeeklyToggleFromServer();
    }}

    function cancelPrefsPanel() {{
      clearPrefsParams();
      hidePanel();
      const prior = loadWelcomePrefs();
      if (prior && prior.completed) applyWelcomePrefs(prior);
      else if (prior) applyWelcomePrefs(prior);
    }}

    function upsertPrefsToServer(payload, weeklyOn) {{
      if (!sbClient || !sbAuthUser) return Promise.resolve();
      const row = {{
        user_id: sbAuthUser.id,
        email: sbAuthUser.email || "",
        stores: payload.retailers.slice(),
        categories: payload.categories.slice(),
        weekly_email: !!weeklyOn,
        deal_alerts: !!weeklyOn,
        unsubscribed_at: weeklyOn ? null : new Date().toISOString(),
      }};
      return sbClient.from("subscribers").upsert(row, {{ onConflict: "user_id" }}).then(function(res) {{
        if (res && res.error) throw res.error;
        if (weeklyOn) markEmailSubscribed();
      }});
    }}

    function savePrefs() {{
      const weeklyOn = !!(weeklyToggle && weeklyToggle.checked);
      const payload = {{
        completed: true,
        retailers: Array.from(draft.retailers),
        categories: Array.from(draft.categories),
        otherStore: draft.otherOn ? String((otherInput && otherInput.value) || draft.otherStore || "").trim() : "",
      }};
      const prior = loadWelcomePrefs() || {{}};
      if (prior.email) payload.email = prior.email;
      if (prior.subscribed) payload.subscribed = true;

      if (saveBtn) {{
        saveBtn.disabled = true;
        saveBtn.textContent = "Saving…";
      }}

      const after = sbAuthUser
        ? upsertPrefsToServer(payload, weeklyOn)
        : Promise.resolve();

      after.then(function() {{
        saveWelcomePrefs(payload);
        applyWelcomePrefs(payload);
        clearPrefsParams();
        if (saveBtn) saveBtn.textContent = "Saved ✓";
        if (saveTimer) window.clearTimeout(saveTimer);
        saveTimer = window.setTimeout(function() {{
          hidePanel();
          if (saveBtn) {{
            saveBtn.disabled = false;
            saveBtn.textContent = "Save preferences";
          }}
          showStoresSavedToast(payload.retailers);
        }}, 2000);
      }}).catch(function(err) {{
        console.error("[SnackBuddy] prefs save failed", err);
        if (saveBtn) {{
          saveBtn.disabled = false;
          saveBtn.textContent = "Save preferences";
        }}
        showAppToast("Couldn’t save preferences. Try again.");
      }});
    }}

    if (otherInput) {{
      otherInput.addEventListener("input", function() {{
        draft.otherStore = String(otherInput.value || "").trim();
      }});
    }}
    if (weeklyToggle) {{
      weeklyToggle.addEventListener("change", function() {{
        draft.weeklyEmail = !!weeklyToggle.checked;
      }});
    }}
    if (saveBtn) {{
      saveBtn.addEventListener("click", function() {{
        savePrefs();
      }});
    }}
    root.querySelectorAll("[data-prefs-cancel]").forEach(function(el) {{
      el.addEventListener("click", function() {{
        cancelPrefsPanel();
      }});
    }});

    if (signupForm && signupForm.dataset.wired !== "1") {{
      signupForm.dataset.wired = "1";
      signupForm.addEventListener("submit", function(ev) {{
        ev.preventDefault();
        if (sbAuthUser) {{
          syncEmailSection();
          loadWeeklyToggleFromServer();
          return;
        }}
        const email = signupEmail ? String(signupEmail.value || "").trim() : "";
        if (!isValidEmail(email)) {{
          if (signupStatus) {{
            signupStatus.textContent = "Enter a valid email address.";
            signupStatus.classList.add("is-error");
          }}
          return;
        }}
        if (!sbClient) {{
          if (signupStatus) {{
            signupStatus.textContent = "Signup is temporarily unavailable. Try again later.";
            signupStatus.classList.add("is-error");
          }}
          return;
        }}
        if (signupSubmit) signupSubmit.disabled = true;
        if (signupStatus) {{
          signupStatus.textContent = "Sending link…";
          signupStatus.classList.remove("is-error");
        }}
        submitMagicLinkSignup(email).then(function() {{
          markEmailSubscribed();
          if (signupStatus) signupStatus.textContent = MAGIC_LINK_MSG;
          if (signupEmail) signupEmail.blur();
        }}).catch(function(err) {{
          console.error("[SnackBuddy] prefs magic link failed", err);
          if (signupStatus) {{
            signupStatus.textContent = "Couldn’t send the link. Try again.";
            signupStatus.classList.add("is-error");
          }}
          if (signupSubmit) signupSubmit.disabled = false;
        }});
      }});
    }}

    openStoresPicker = function() {{
      openPrefsPanel();
    }};

    const storesLineChange = document.getElementById("sb-stores-line-change");
    if (storesLineChange && storesLineChange.dataset.wired !== "1") {{
      storesLineChange.dataset.wired = "1";
      storesLineChange.addEventListener("click", function() {{
        openStoresPicker();
      }});
    }}

    // Refresh email section if auth changes while open.
    const prevSync = syncWelcomeEditHint;
    syncWelcomeEditHint = function() {{
      if (typeof prevSync === "function") prevSync();
      if (!open) return;
      syncEmailSection();
      loadWeeklyToggleFromServer();
    }};

    if (wantsPrefsPanel()) {{
      openPrefsPanel();
    }}
  }}

  function wireMyItemsNotifySignup() {{
    const form = document.getElementById("sb-my-items-notify-form");
    const emailInput = document.getElementById("sb-my-items-notify-email");
    const submitBtn = document.getElementById("sb-my-items-notify-submit");
    const statusEl = document.getElementById("sb-my-items-notify-status");
    if (!form || form.dataset.wired === "1") return;
    form.dataset.wired = "1";

    function setNotifyStatus(msg, isError) {{
      if (!statusEl) return;
      statusEl.textContent = msg || "";
      statusEl.classList.toggle("is-error", !!isError);
    }}

    form.addEventListener("submit", function(ev) {{
      ev.preventDefault();
      if (sbAuthUser) {{
        if (submitBtn) submitBtn.disabled = true;
        setNotifyStatus("Re-enabling emails…");
        resubscribeSignedInUser().then(function(ok) {{
          setNotifyStatus(ok
            ? "You’re back on the list — emails re-enabled."
            : "Couldn’t update preferences. Try again.", !ok);
          syncMyItemsNotifyVisibility();
          if (!ok && submitBtn) submitBtn.disabled = false;
        }});
        return;
      }}
      if (hasEmailSubscription()) {{
        setNotifyStatus(MAGIC_LINK_MSG);
        return;
      }}
      const email = emailInput ? String(emailInput.value || "").trim() : "";
      if (!isValidEmail(email)) {{
        setNotifyStatus("Enter a valid email address.", true);
        if (emailInput) emailInput.focus();
        return;
      }}
      if (!sbClient) {{
        setNotifyStatus("Signup is temporarily unavailable. Try again later.", true);
        return;
      }}
      if (submitBtn) submitBtn.disabled = true;
      setNotifyStatus("Sending link…");
      submitMagicLinkSignup(email).then(function() {{
        markEmailSubscribed();
        trackEvent("generate_lead", {{ method: "my_items_notify" }});
        setNotifyStatus(MAGIC_LINK_MSG);
        if (emailInput) emailInput.blur();
        window.setTimeout(syncMyItemsNotifyVisibility, 1600);
      }}).catch(function(err) {{
        console.error("[SnackBuddy] my-items magic link failed", err);
        setNotifyStatus("Couldn’t send the link. Try again or use the footer form.", true);
        if (submitBtn) submitBtn.disabled = false;
      }});
    }});
  }}

  function wireFooterSignup() {{
    const form = document.getElementById("sb-footer-signup");
    const emailInput = document.getElementById("sb-footer-email");
    const submitBtn = document.getElementById("sb-footer-email-submit");
    const statusEl = document.getElementById("sb-footer-email-status");
    if (!form || form.dataset.wired === "1") return;
    form.dataset.wired = "1";

    function setFooterStatus(msg, isError) {{
      if (!statusEl) return;
      statusEl.textContent = msg || "";
      statusEl.classList.toggle("is-error", !!isError);
    }}

    form.addEventListener("submit", function(ev) {{
      ev.preventDefault();
      if (sbAuthUser) {{
        if (submitBtn) submitBtn.disabled = true;
        setFooterStatus("Re-enabling emails…");
        resubscribeSignedInUser().then(function(ok) {{
          setFooterStatus(ok
            ? "You’re back on the list — emails re-enabled."
            : "Couldn’t update preferences. Try again.", !ok);
          if (!ok && submitBtn) submitBtn.disabled = false;
        }});
        return;
      }}
      const email = emailInput ? String(emailInput.value || "").trim() : "";
      if (!isValidEmail(email)) {{
        setFooterStatus("Enter a valid email address.", true);
        if (emailInput) emailInput.focus();
        return;
      }}
      if (!sbClient) {{
        setFooterStatus("Signup is temporarily unavailable. Try again later.", true);
        return;
      }}
      if (submitBtn) submitBtn.disabled = true;
      setFooterStatus("Sending link…");
      submitMagicLinkSignup(email).then(function() {{
        markEmailSubscribed();
        syncMyItemsNotifyVisibility();
        trackEvent("generate_lead", {{ method: "footer" }});
        setFooterStatus(MAGIC_LINK_MSG);
        if (emailInput) emailInput.blur();
      }}).catch(function(err) {{
        console.error("[SnackBuddy] footer magic link failed", err);
        setFooterStatus("Couldn’t send the link. Try again in a moment.", true);
        if (submitBtn) submitBtn.disabled = false;
      }});
    }});
  }}

  initSupabaseAuth();
  initWelcomeFlow();
  initPrefsPanel();
  wireMyItemsNotifySignup();
  wireFooterSignup();
  wireEmailAskBanner();
  wireSavingsPanel();

  filterMasterBtn?.addEventListener("click", function(e) {{
    e.stopPropagation();
    if (filterSheet?.classList.contains("open")) closeFilterSheet();
    else openFilterSheet();
  }});

  filterSheetClose?.addEventListener("click", closeFilterSheet);
  filterSheetDone?.addEventListener("click", closeFilterSheet);
  filterSheetClear?.addEventListener("click", clearAllFilters);
  filterSheetBackdrop?.addEventListener("click", closeFilterSheet);
  filterDropdownBackdrop?.addEventListener("click", closeStoresPopover);

  storesChip?.addEventListener("click", function(e) {{
    e.stopPropagation();
    if (storesPopoverOpen) closeStoresPopover();
    else openStoresPopover();
  }});

  myItemsExitChip?.addEventListener("click", function() {{
    if (myItemsView) setTierSelection("my-items");
  }});

  legendToggle?.addEventListener("click", function() {{
    if (!badgeLegend) return;
    const open = badgeLegend.hidden;
    badgeLegend.hidden = !open;
    legendToggle.setAttribute("aria-expanded", open ? "true" : "false");
  }});

  chipRowTrack?.addEventListener("scroll", function() {{
    updateChipRowScroll();
    if (storesPopoverOpen) positionStoresPopover();
  }});
  window.addEventListener("resize", function() {{
    updateChipRowScroll();
    if (storesPopoverOpen) positionStoresPopover();
  }});
  updateChipRowScroll();

  document.addEventListener("keydown", function(ev) {{
    if (ev.key === "Escape") {{
      closeStoresPopover();
      closeFilterSheet();
    }}
  }});
  /* ============================
     CARD IMAGE CAROUSEL (per flavor)
     ============================ */
  function initCardCarousels(root) {{
    const scope = root || document;
    scope.querySelectorAll(".card-carousel").forEach(function(carousel) {{
      if (carousel.dataset.carouselReady === "1") return;
      const slides = Array.from(carousel.querySelectorAll(".card-carousel-slide"));
      if (!slides.length) return;

      carousel.dataset.carouselReady = "1";

      let index = 0;
      const caption = carousel.querySelector(".card-carousel-caption");
      const stage = carousel.querySelector(".card-carousel-stage");

      function updateCaption() {{
        if (!caption) return;
        const name = slides[index].getAttribute("data-flavor-name") || "";
        caption.textContent = name;
        caption.hidden = !name;
      }}

      if (slides.length === 1) {{
        slides[0].classList.add("is-active");
        updateCaption();
        return;
      }}

      const prevBtn = carousel.querySelector(".card-carousel-prev");
      const nextBtn = carousel.querySelector(".card-carousel-next");

      function render() {{
        const n = slides.length;
        slides.forEach(function(slide, i) {{
          slide.classList.remove("is-active", "is-prev", "is-next", "is-hidden");
          if (i === index) {{
            slide.classList.add("is-active");
          }} else if (i === (index - 1 + n) % n) {{
            slide.classList.add("is-prev");
          }} else if (i === (index + 1) % n) {{
            slide.classList.add("is-next");
          }} else {{
            slide.classList.add("is-hidden");
          }}
        }});
        updateCaption();
      }}

      function goPrev() {{
        index = (index - 1 + slides.length) % slides.length;
        render();
      }}

      function goNext() {{
        index = (index + 1) % slides.length;
        render();
      }}

      prevBtn?.addEventListener("click", function(e) {{
        e.preventDefault();
        e.stopPropagation();
        goPrev();
      }});

      nextBtn?.addEventListener("click", function(e) {{
        e.preventDefault();
        e.stopPropagation();
        goNext();
      }});

      if (stage) {{
        let touchStartX = 0;
        let touchStartY = 0;
        stage.addEventListener("touchstart", function(e) {{
          if (!e.changedTouches.length) return;
          touchStartX = e.changedTouches[0].screenX;
          touchStartY = e.changedTouches[0].screenY;
        }}, {{ passive: true }});
        stage.addEventListener("touchend", function(e) {{
          if (!e.changedTouches.length) return;
          const dx = e.changedTouches[0].screenX - touchStartX;
          const dy = e.changedTouches[0].screenY - touchStartY;
          if (Math.abs(dx) < 36 || Math.abs(dx) < Math.abs(dy)) return;
          if (dx < 0) goNext();
          else goPrev();
        }}, {{ passive: true }});
      }}

      render();
    }});
  }}

  initCardCarousels(document);

  /* ============================
     FLAVOR EXPAND/COLLAPSE
     ============================ */
  const flavorExpandLinks = document.querySelectorAll(".flavor-expand-link");
  flavorExpandLinks.forEach(function(link) {{
    link.addEventListener("click", function() {{
      const cardId = this.getAttribute("data-card-id");
      const expandedList = document.getElementById("flavors-" + cardId);
      if (!expandedList) return;

      const isExpanded = this.getAttribute("aria-expanded") === "true";
      
      if (isExpanded) {{
        // Collapse
        expandedList.style.display = "none";
        this.setAttribute("aria-expanded", "false");
        const n = this.getAttribute("data-extra-count");
        if (n) {{
          this.textContent = n + " more flavors";
        }}
      }} else {{
        // Expand
        expandedList.style.display = "flex";
        this.setAttribute("aria-expanded", "true");
        this.textContent = "Show less";
      }}
    }});
  }});
}});
</script>

</body>
</html>
    """
def main():
    deals = load_deals()
    expired = [d for d in deals if is_expired_promo(d)]
    if expired:
        print(f"EXPIRED_PROMO: dropped {len(expired)} deal(s) with past promo_end")
        deals = [d for d in deals if not is_expired_promo(d)]
    page_html = build_page_html(deals)

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(page_html, encoding="utf-8")
    print("SCRIPT FILE:", Path(__file__).resolve())
    print("OUTPUT PATH:", OUTPUT_PATH.resolve())
    print("WROTE:", OUTPUT_PATH.resolve())
    print("DEALS:", len(deals))
    print("SUPABASE:", SUPABASE_URL)

if __name__ == "__main__":
    main()

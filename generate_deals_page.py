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
_BUNDLE_TITLE_RE = re.compile(r"^\(\s*(\d+)\s*packs?\s*\)", re.I)
# Flat bundle price for this many calendar days → everyday multipack saving, not a deal.
EVERYDAY_BUNDLE_STABLE_DAYS = 30
_BUNDLE_PRICE_MATCH_TOL = 0.01  # cents-level scrape jitter still counts as "same"
# Structural multipack: baseline within 2% of N × smaller listing's current price.
_BUNDLE_BASELINE_VS_MULTIPLE_PCT = 0.02
_AISLE_DETAIL_RE = re.compile(r"(?:^|;)\s*aisle=(True|False)\b", re.I)
_AISLE_LABEL_DETAIL_RE = re.compile(r"(?:^|;)\s*aisle_label=([^;]*)", re.I)

# Beehiiv IDs — from beehiiv Settings / Subscribe forms.
# Prefer env / sibling snack-buddy .env so IDs aren't hardcoded.
def _load_env_value(*keys: str) -> str:
    for key in keys:
        val = os.environ.get(key, "").strip()
        if val:
            return val
    for env_path in (ROOT / ".env", ROOT.parent / "snack-buddy" / ".env"):
        if not env_path.is_file():
            continue
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() in keys:
                    val = v.strip().strip('"').strip("'")
                    if val:
                        return val
        except OSError:
            pass
    return ""


BEEHIIV_PUBLICATION_ID = _load_env_value("BEEHIIV_PUBLICATION_ID", "BEEHIIV_PUB_ID")
# Official v3 subscribe form (Audience → Subscribe forms → Get embed code)
BEEHIIV_FORM_ID = _load_env_value("BEEHIIV_FORM_ID") or "fe019cbd-c068-4f4a-a458-757e07b5460a"

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
    """Title '(N pack)' first; else structural multipack N."""
    return walmart_title_bundle_n(deal, walmart_ctx) or walmart_structural_bundle_n(
        deal, walmart_ctx
    )


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
    Mark Walmart multipack listings (title '(N pack)' or structural pack
    multiple) for everyday-bundle treatment.

    Title bundles: everyday when own price flat 30+ days.
    Structural bundles: everyday multipack saving vs singles, unless the
    listing dropped below its own long-held usual price (then a normal deal).

    Sets deal['is_everyday_bundle'] / deal['bundle_n'] / deal['bundle_source'].
    Does not change prices or baselines.
    """
    bundle_meta: list[tuple[dict, int, str, float, str]] = []
    skus: set[str] = set()
    for deal in deals:
        title_n = walmart_title_bundle_n(deal, walmart_ctx)
        structural_n = None if title_n else walmart_structural_bundle_n(
            deal, walmart_ctx
        )
        n = title_n or structural_n
        source = "title" if title_n else ("structural" if structural_n else "")
        if not n:
            deal["is_everyday_bundle"] = False
            deal["bundle_n"] = None
            deal["bundle_source"] = None
            continue
        try:
            price = float(deal.get("new_price"))
        except (TypeError, ValueError):
            deal["is_everyday_bundle"] = False
            deal["bundle_n"] = n
            deal["bundle_source"] = source
            continue
        deal["bundle_n"] = n
        deal["bundle_source"] = source
        sku = walmart_listing_sku(deal)
        if sku:
            deal["retailer_sku"] = deal.get("retailer_sku") or sku
            skus.add(sku)
            bundle_meta.append((deal, n, sku, price, source))
        else:
            deal["is_everyday_bundle"] = False

    history = load_walmart_price_history_by_sku(skus)
    for deal, n, sku, price, source in bundle_meta:
        hist = history.get(sku) or {}
        if source == "title":
            deal["is_everyday_bundle"] = is_everyday_bundle_price(hist, price)
        else:
            # Structural: badge unless this is a drop below a long-held usual.
            if is_everyday_bundle_price(hist, price):
                deal["is_everyday_bundle"] = True
            else:
                deal["is_everyday_bundle"] = not is_drop_below_usual_bundle_price(
                    hist, price
                )
    return deals


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
) -> tuple[bool | None, bool | None, bool | None]:
    """
    Return (aisle, pickup, shipping) for a Walmart deal.
    Prefers scrape context (detail aisle= + tile flags); falls back to deal fields.
    """
    aisle = None
    pickup = _truthy_csv_flag(deal.get("pickup_available"))
    shipping = _truthy_csv_flag(deal.get("shipping_available"))
    if walmart_ctx:
        url = _norm_url_key(deal.get("retailer_url") or deal.get("canonical_url") or "")
        meta = (walmart_ctx.get("by_url") or {}).get(url) or {}
        if meta.get("aisle") is not None:
            aisle = bool(meta.get("aisle"))
        if meta.get("pickup_available") is not None:
            pickup = bool(meta.get("pickup_available"))
        if meta.get("shipping_available") is not None:
            shipping = bool(meta.get("shipping_available"))
    return aisle, pickup, shipping


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

    # Walmart multipack (title '(N pack)' or structural pack multiple).
    if "walmart" in retailer_l:
        n = deal.get("bundle_n") or walmart_bundle_n(deal, walmart_ctx)
        if n:
            try:
                n_int = int(n)
            except (TypeError, ValueError):
                n_int = 0
            if n_int > 0:
                text = f"Order online (pickup or delivery) · bundle of {n_int}"
                each = single_each_in_store_price(deal, n_int, walmart_ctx)
                if each is not None:
                    text += f" · ${each:.2f} each if bought separately in store"
                return text, "availability online-only"

        # Aisle on the PDP = on-shelf in store. Pickup alone is an online order.
        aisle, pickup, shipping = _walmart_fulfillment_flags(deal, walmart_ctx)
        if walmart_ctx and walmart_ctx.get("has_aisle_data") and aisle is not None:
            if aisle and (pickup or shipping or shipping is None):
                return "In store & online", "availability both"
            if aisle and shipping is False and pickup is False:
                return "In store only", "availability in-store"
            if (not aisle) and pickup:
                return "Order online · pickup or delivery", "availability online-only"
            if (not aisle) and shipping and not pickup:
                return "Online only · ships from Walmart", "availability online-only"
            if (not aisle) and shipping:
                return "Online only · ships from Walmart", "availability online-only"

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
    Everyday Walmart bundles are "bundle" (not in deal-tier chips).
    """
    if deal and deal.get("is_everyday_bundle"):
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
    if deal.get("is_everyday_bundle"):
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
    is_everyday_bundle = bool(deal.get("is_everyday_bundle"))

    # Tier for filtering (everyday bundles are not deal tiers)
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
            <div class="sb-last-sale-lines" hidden></div>
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
    beehiiv_form_id = html.escape(BEEHIIV_FORM_ID)
    beehiiv_pub_id = html.escape(BEEHIIV_PUBLICATION_ID)
    beehiiv_embed_script = (
        f'<script async src="https://subscribe-forms.beehiiv.com/v3/loader.js" '
        f'data-beehiiv-form="{beehiiv_form_id}"></script>'
        if BEEHIIV_FORM_ID
        else '<p class="sb-note">Email signup is almost ready.</p>'
    )

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

@media (min-width: 900px) {{
  .sb-filter-bar-section {{
    display: none;
  }}

  .sb-filter-master {{
    width: auto;
    padding: 0 14px 0 12px;
  }}

  .sb-filter-master-label {{
    display: inline;
  }}

  .sb-results-count {{
    display: inline;
  }}

  .sb-deals-shell {{
    display: grid;
    grid-template-columns: 250px minmax(0, 1fr);
    gap: 22px;
    align-items: start;
  }}

  .sb-deals-shell.is-filters-collapsed {{
    grid-template-columns: minmax(0, 1fr);
  }}

  .sb-filter-sidebar {{
    display: block;
    position: sticky;
    top: 72px;
    max-height: calc(100vh - 88px);
    overflow: auto;
    background: #ffffff;
    border: 1px solid var(--border-subtle);
    border-radius: 14px;
    padding: 12px 12px 14px;
    box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
  }}

  .sb-deals-shell.is-filters-collapsed .sb-filter-sidebar {{
    display: none;
  }}
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
        .card.card-off-sale {{
            opacity: 0.92;
            background: #f8fafc;
        }}
        .card.card-off-sale .card-pricing .new-price {{
            color: var(--text-muted);
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
        .sb-my-items-summary {{
            margin: 0 0 12px 0;
            padding: 12px 14px;
            border-radius: 12px;
            background: #fffbeb;
            border: 1px solid #fde68a;
            color: #92400e;
            font-size: 14px;
            font-weight: 600;
        }}
        .sb-my-items-notify {{
            margin: 0 0 14px 0;
            padding: 14px 16px;
            border-radius: 12px;
            background: #ffffff;
            border: 1px solid var(--border-subtle);
            box-shadow: var(--shadow-soft);
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
        .sb-my-items-notify-frame {{
            position: absolute;
            width: 0;
            height: 0;
            border: 0;
            overflow: hidden;
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
            font-size: 14px;
            margin: 4px 0 10px;
            color: #374151;
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
            width: min(620px, 100%);
            margin: auto 16px;
            max-height: min(92vh, 780px);
            overflow: auto;
            padding: 28px 24px 22px;
            border-radius: 24px;
            background:
                linear-gradient(180deg, rgba(255,255,255,0.98), rgba(239, 246, 255, 0.94));
            border: 1px solid rgba(37, 99, 235, 0.12);
            box-shadow: 0 30px 80px rgba(15, 23, 42, 0.22);
            font-family: "Outfit", system-ui, sans-serif;
            transform: translateY(18px) scale(0.98);
            transition: transform 0.4s cubic-bezier(0.22, 1, 0.36, 1);
        }}
        .sb-welcome.open .sb-welcome-panel {{
            transform: translateY(0) scale(1);
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
            animation: sbWelcomeIn 0.4s ease both;
        }}
        .sb-welcome-step.is-active {{
            display: block;
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
            margin-bottom: 18px;
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
            margin-bottom: 18px;
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
        .sb-beehiiv-embed {{
            margin-top: 10px;
            width: 100%;
            max-width: 520px;
            min-height: 120px;
        }}
        .sb-beehiiv-embed iframe {{
            max-width: 100% !important;
        }}
        .sb-welcome-signup {{
            margin: 8px 0 4px;
            width: 100%;
        }}
        .sb-welcome-signup-frame {{
            position: absolute;
            width: 0;
            height: 0;
            border: 0;
            overflow: hidden;
            clip: rect(0, 0, 0, 0);
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

    <a href="#deals-list" class="sb-nav-link">All Deals</a>
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
                </div>
            </div>
        </section>

        {best_savings_html}

        <section class="sb-tools-bar" aria-label="Search and sort deals">
            <div class="sb-filter-toggle-wrap">
                <button type="button" class="sb-filter-master" id="sb-filter-master" aria-label="Filters" aria-expanded="true" aria-controls="sb-filter-sidebar">
                    <svg class="sb-filter-master-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                        <path fill="currentColor" d="M4.5 5.75c0-.69.56-1.25 1.25-1.25h12.5c.69 0 1.25.56 1.25 1.25v.51c0 .24-.07.47-.2.67l-5.2 7.28v5.04c0 .41-.25.78-.63.92l-2.45 1.02c-.77.32-1.63-.24-1.63-1.08v-6.9L4.7 6.93c-.13-.2-.2-.43-.2-.67v-.51z"/>
                    </svg>
                    <span class="sb-filter-master-label" id="sb-filter-master-label">Hide Filters</span>
                    <span class="sb-filter-master-badge" id="sb-filter-master-count" hidden>0</span>
                </button>
            </div>
            <div class="sb-search-wrap" id="sb-search-wrap">
                <label class="visually-hidden" for="sb-search-input">Search deals</label>
                <input
                    type="search"
                    id="sb-search-input"
                    class="sb-search-input"
                    placeholder="Search brand, product, or flavor…"
                    autocomplete="off"
                    enterkeyhint="search"
                />
                <button type="button" class="sb-search-clear" id="sb-search-clear" aria-label="Clear search">&times;</button>
            </div>
            <div class="sb-results-count" id="sb-results-count" aria-live="polite"></div>
            <div class="sb-sort-wrap">
                <button type="button" class="sb-sort-pill" id="sb-sort-pill" aria-haspopup="listbox" aria-expanded="false" aria-controls="sb-sort-menu">
                    Sort <span class="sb-filter-pill-chevron" aria-hidden="true">▾</span>
                </button>
                <div class="sb-sort-menu" id="sb-sort-menu" role="listbox" aria-label="Sort deals" hidden>
                    <button type="button" class="sb-sort-option is-selected" role="option" data-sort="best" aria-selected="true">Best discount (default)</button>
                    <button type="button" class="sb-sort-option" role="option" data-sort="pct-desc" aria-selected="false">% off: high to low</button>
                    <button type="button" class="sb-sort-option" role="option" data-sort="saved-desc" aria-selected="false">Most $ saved</button>
                    <button type="button" class="sb-sort-option" role="option" data-sort="price-asc" aria-selected="false">Price: low to high</button>
                    <button type="button" class="sb-sort-option" role="option" data-sort="price-desc" aria-selected="false">Price: high to low</button>
                </div>
                <select id="sb-sort-select" class="sb-sort-select" aria-hidden="true" tabindex="-1">
                    <option value="best" selected>Best discount (default)</option>
                    <option value="pct-desc">% off: high to low</option>
                    <option value="saved-desc">Most $ saved</option>
                    <option value="price-asc">Price: low to high</option>
                    <option value="price-desc">Price: high to low</option>
                </select>
            </div>
        </section>

        <section class="sb-tier-chips-section" aria-label="Filter by deal strength">
            <div class="sb-tier-chips-scroll" id="sb-tier-chips-scroll">
                <button type="button" class="sb-filter-scroll-btn sb-filter-scroll-prev" id="sb-tier-scroll-prev" aria-label="Scroll deal tiers left">&#8249;</button>
                <div class="sb-tier-chips-track" id="sb-tier-chips-track">
                    <div class="sb-tier-chips">
                        <span class="sb-tier-chips-label">Show</span>
                        <button type="button" class="sb-tier-chip sb-my-items-chip" id="sb-my-items-chip" data-tier-filter="my-items" aria-pressed="false" title="Starred items">★ My Items</button>
                        <button type="button" class="sb-tier-chip is-active" data-tier-filter="all" aria-pressed="true">All</button>
                        <button type="button" class="sb-tier-chip" data-tier-filter="diamond" aria-pressed="false" title="25% off and up">💎 Diamond</button>
                        <button type="button" class="sb-tier-chip" data-tier-filter="fire" aria-pressed="false" title="20–24% off">🔥 Fire</button>
                        <button type="button" class="sb-tier-chip" data-tier-filter="strong" aria-pressed="false" title="10–19% off">💪 Strong</button>
                    </div>
                </div>
                <button type="button" class="sb-filter-scroll-btn sb-filter-scroll-next" id="sb-tier-scroll-next" aria-label="Scroll deal tiers right">&#8250;</button>
            </div>
        </section>
        <div class="sb-my-items-summary" id="sb-my-items-summary" hidden></div>
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
            <iframe
                class="sb-my-items-notify-frame"
                id="sb-my-items-notify-frame"
                name="sb-my-items-notify-frame"
                title="Subscribe helper"
            ></iframe>
        </div>

        <section class="sb-filter-bar-section" aria-label="Deal filters">
            <div class="sb-filter-bar">
                <div class="sb-filter-bar-scroll" id="sb-filter-bar-scroll">
                    <button type="button" class="sb-filter-scroll-btn sb-filter-scroll-prev" id="sb-filter-scroll-prev" aria-label="Scroll filters left">&#8249;</button>
                    <div class="sb-filter-bar-track" id="sb-filter-bar-track">
                        <div class="sb-filter-bar-inner" id="sb-filter-bar-inner"></div>
                    </div>
                    <button type="button" class="sb-filter-scroll-btn sb-filter-scroll-next" id="sb-filter-scroll-next" aria-label="Scroll filters right">&#8250;</button>
                </div>
            </div>
        </section>
        <div class="sb-filter-dropdown-backdrop" id="sb-filter-dropdown-backdrop"></div>
        <div class="sb-filter-dropdown-panel" id="sb-filter-dropdown-panel" hidden role="dialog" aria-modal="true"></div>
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
        <aside class="sb-filter-sidebar" id="sb-filter-sidebar" aria-label="Deal filters">
            <div class="sb-filter-sidebar-head">
                <div class="sb-filter-sidebar-title">Filters</div>
                <button type="button" class="sb-filter-sidebar-clear" id="sb-filter-sidebar-clear">Clear all</button>
            </div>
            <div id="sb-filter-sidebar-body"></div>
        </aside>
        <div class="sb-deals-main">
        <section class="card-grid" id="deals-list">
            {cards_html}
        </section>
        <div class="sb-deals-empty" id="sb-deals-empty" role="status" aria-live="polite">
            No deals match your search or filters. Try clearing search or filters.
        </div>
        <div class="sb-my-items-empty" id="sb-my-items-empty" role="status" aria-live="polite">
            No saved items yet. Tap the ★ on any deal card to save it here — we’ll track when it goes on sale at each store.
        </div>

        </div>
        </div>

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
                    We’ll email when the good stuff drops. Your filters stay either way.
                </p>

                <div class="sb-beehiiv-embed" id="sb-beehiiv-footer">
                    {beehiiv_embed_script}
                </div>

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
        <div class="sb-welcome-progress" aria-hidden="true">
          <span class="sb-welcome-dot is-on" data-dot="0"></span>
          <span class="sb-welcome-dot" data-dot="1"></span>
          <span class="sb-welcome-dot" data-dot="2"></span>
          <span class="sb-welcome-dot" data-dot="3"></span>
        </div>

        <section class="sb-welcome-step is-active" data-step="0">
          <div class="sb-welcome-kicker">Better snacks. Smarter buys.</div>
          <h1 class="sb-welcome-brand" id="sb-welcome-brand">Snack<span>Buddy</span></h1>
          <p class="sb-welcome-lead">Your better-for-you deals, cut down to the stores and snacks you actually buy.</p>
          <div class="sb-welcome-actions">
            <button type="button" class="sb-welcome-btn" data-welcome-next>Personalize my deals</button>
            <button type="button" class="sb-welcome-btn sb-welcome-btn-ghost" data-welcome-skip>Just show everything</button>
          </div>
        </section>

        <section class="sb-welcome-step" data-step="1">
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
          <div class="sb-welcome-actions">
            <button type="button" class="sb-welcome-btn" data-welcome-next>Next</button>
            <button type="button" class="sb-welcome-btn sb-welcome-btn-ghost" data-welcome-back>Back</button>
          </div>
        </section>

        <section class="sb-welcome-step" data-step="2">
          <h2 class="sb-welcome-step-title">What’s usually in your pantry?</h2>
          <p class="sb-welcome-step-text">Pick the categories you restock most, or leave them off to keep everything.</p>
          <div class="sb-welcome-chips" id="sb-welcome-categories"></div>
          <p class="sb-welcome-preview" id="sb-welcome-preview-categories"></p>
          <div class="sb-welcome-actions">
            <button type="button" class="sb-welcome-btn" data-welcome-next>See my deals</button>
            <button type="button" class="sb-welcome-btn sb-welcome-btn-ghost" data-welcome-back>Back</button>
          </div>
        </section>

        <section class="sb-welcome-step" data-step="3">
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
          <iframe
            class="sb-welcome-signup-frame"
            id="sb-welcome-signup-frame"
            name="sb-welcome-signup-frame"
            title="Subscribe helper"
          ></iframe>
          <p class="sb-welcome-note">No spam. Unsubscribe anytime. Prefs stay on this device.</p>
          <div class="sb-welcome-actions">
            <button type="button" class="sb-welcome-btn" data-welcome-next>See my deals</button>
            <button type="button" class="sb-welcome-btn sb-welcome-btn-ghost" data-welcome-skip>Skip for now</button>
          </div>
        </section>
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

    <!-- Tiny JS for slide-out menu -->
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

  const filterBarInner = document.getElementById("sb-filter-bar-inner");
  const filterSheetBody = document.getElementById("sb-filter-sheet-body");
  const filterMasterBtn = document.getElementById("sb-filter-master");
  const filterMasterLabel = document.getElementById("sb-filter-master-label");
  const filterMasterCount = document.getElementById("sb-filter-master-count");
  const dealsShell = document.getElementById("sb-deals-shell");
  const filterSidebar = document.getElementById("sb-filter-sidebar");
  const filterSidebarBody = document.getElementById("sb-filter-sidebar-body");
  const filterSidebarClear = document.getElementById("sb-filter-sidebar-clear");
  const resultsCount = document.getElementById("sb-results-count");
  const filterDropdownBackdrop = document.getElementById("sb-filter-dropdown-backdrop");
  const filterDropdownPanel = document.getElementById("sb-filter-dropdown-panel");
  const filterSheet = document.getElementById("sb-filter-sheet");
  const filterSheetBackdrop = document.getElementById("sb-filter-sheet-backdrop");
  const filterSheetClose = document.getElementById("sb-filter-sheet-close");
  const filterSheetClear = document.getElementById("sb-filter-sheet-clear");
  const filterSheetDone = document.getElementById("sb-filter-sheet-done");
  const filterTrack = document.getElementById("sb-filter-bar-track");
  const filterBarScroll = document.getElementById("sb-filter-bar-scroll");
  const filterScrollPrev = document.getElementById("sb-filter-scroll-prev");
  const filterScrollNext = document.getElementById("sb-filter-scroll-next");
  const tierTrack = document.getElementById("sb-tier-chips-track");
  const tierBarScroll = document.getElementById("sb-tier-chips-scroll");
  const tierScrollPrev = document.getElementById("sb-tier-scroll-prev");
  const tierScrollNext = document.getElementById("sb-tier-scroll-next");

  function isDesktopSidebar() {{
    return window.matchMedia("(min-width: 900px)").matches;
  }}

  function updateFilterToggleLabel() {{
    if (!filterMasterLabel) return;
    if (!isDesktopSidebar()) {{
      filterMasterLabel.textContent = "Filters";
      filterMasterBtn?.setAttribute("aria-expanded", filterSheet?.classList.contains("open") ? "true" : "false");
      return;
    }}
    const collapsed = !!dealsShell?.classList.contains("is-filters-collapsed");
    filterMasterLabel.textContent = collapsed ? "Show Filters" : "Hide Filters";
    filterMasterBtn?.setAttribute("aria-expanded", collapsed ? "false" : "true");
  }}

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
  if (!grid || !cards.length || !filterBarInner) {{
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

  const selected = {{
    retailer: new Set(),
    section: new Set(),
    category: new Set(),
    brand: new Set(),
  }};
  let selectedTiers = new Set();
  let myItemsView = false;
  const MY_ITEMS_KEY = "sb_my_items_v1";
  const EMAIL_SUB_KEY = "sb_email_subscribed_v1";
  const myItemsSummary = document.getElementById("sb-my-items-summary");
  const myItemsNotify = document.getElementById("sb-my-items-notify");
  const myItemsEmpty = document.getElementById("sb-my-items-empty");
  const offSaleCards = new Map();
  let lastSaleItems = {{}};

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
  }}
  function syncMyItemsNotifyVisibility() {{
    if (!myItemsNotify) return;
    const show = myItemsView && !hasEmailSubscription();
    myItemsNotify.hidden = !show;
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

  function fillLastSaleLines(card) {{
    const box = card.querySelector(".sb-last-sale-lines");
    if (!box) return;
    if (!myItemsView) {{
      box.hidden = true;
      box.innerHTML = "";
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
  }}

  function ensureOffSaleCard(itemId) {{
    if (offSaleCards.has(itemId)) return offSaleCards.get(itemId);
    const meta = lastSaleItems[itemId];
    if (!meta) return null;
    const card = document.createElement("div");
    card.className = "card card-off-sale";
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
      '<div class="card-image-wrap"></div>' +
      '<div class="card-content">' +
      '<div class="card-pricing"><span class="new-price">Not on sale</span></div>' +
      '<div class="card-title"></div>' +
      '<div class="sb-last-sale-lines"></div>' +
      '</div>';
    card.querySelector(".card-title").textContent = meta.product_name || meta.brand || "Saved item";
    offSaleCards.set(itemId, card);
    return card;
  }}

  function toggleStar(itemId, itemName) {{
    if (!itemId) return;
    if (starredIds.has(itemId)) {{
      starredIds.delete(itemId);
      trackEvent("star_remove", {{ item_id: itemId, item_name: itemName || "" }});
    }} else {{
      starredIds.add(itemId);
      trackEvent("star_add", {{ item_id: itemId, item_name: itemName || "" }});
    }}
    saveStarredIds();
    syncStarButtons();
    if (myItemsView) applyFiltersAndSort();
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
      const nameEl = host.querySelector(".card-title, .sb-savings-tile-name");
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

  const pillWraps = {{}};
  let openDropdownKey = null;

  function cardMatchesTier(card) {{
    if (myItemsView) return true;
    if (selectedTiers.size === 0) return true;
    const t = card.dataset.tier || "sale";
    return selectedTiers.has(t);
  }}

  function syncTierChips() {{
    document.querySelectorAll(".sb-tier-chip[data-tier-filter]").forEach(function(el) {{
      const mode = el.getAttribute("data-tier-filter") || "all";
      let active = false;
      if (mode === "my-items") active = myItemsView;
      else if (myItemsView) active = false;
      else if (mode === "all") active = selectedTiers.size === 0;
      else active = selectedTiers.has(mode);
      el.classList.toggle("is-active", active);
      if (el.hasAttribute("aria-pressed")) el.setAttribute("aria-pressed", active ? "true" : "false");
    }});
  }}

  function setTierSelection(mode) {{
    if (mode === "my-items") {{
      myItemsView = !myItemsView;
      if (myItemsView) {{
        selectedTiers.clear();
        trackEvent("my_items_open", {{}});
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
    if (excludeKey !== "retailer" && selected.retailer.size > 0 && !selected.retailer.has(r)) return false;
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

  function pruneInvalidSelections() {{
    FILTER_DEFS.forEach(function(def) {{
      const available = new Set(getSmartOptionsForKey(def.key).map(function(opt) {{ return opt.value; }}));
      Array.from(selected[def.key]).forEach(function(val) {{
        if (!available.has(val)) selected[def.key].delete(val);
      }});
    }});
  }}

  function totalSelectedCount() {{
    return selected.retailer.size + selected.section.size
      + selected.category.size + selected.brand.size;
  }}

  function updateBadges() {{
    FILTER_DEFS.forEach(function(def) {{
      const wrap = pillWraps[def.key];
      if (!wrap) return;
      const badge = wrap.querySelector(".sb-filter-pill-badge");
      const n = selected[def.key].size;
      if (!badge) return;
      if (n > 0) {{
        badge.textContent = String(n);
        badge.hidden = false;
      }} else {{
        badge.hidden = true;
      }}
    }});
    const total = totalSelectedCount();
    if (filterMasterCount) {{
      if (total > 0) {{
        filterMasterCount.textContent = String(total);
        filterMasterCount.hidden = false;
      }} else {{
        filterMasterCount.hidden = true;
      }}
    }}
  }}

  function syncCheckboxes(key) {{
    document.querySelectorAll('input[data-filter-key="' + key + '"]').forEach(function(cb) {{
      cb.checked = selected[key].has(cb.value);
    }});
  }}

  function syncAllCheckboxes() {{
    FILTER_DEFS.forEach(function(def) {{ syncCheckboxes(def.key); }});
  }}

  function closeAllDropdowns() {{
    openDropdownKey = null;
    Object.keys(pillWraps).forEach(function(k) {{
      pillWraps[k].classList.remove("is-open");
      const btn = pillWraps[k].querySelector(".sb-filter-pill");
      if (btn) btn.setAttribute("aria-expanded", "false");
    }});
    if (filterDropdownPanel) {{
      filterDropdownPanel.hidden = true;
      filterDropdownPanel.innerHTML = "";
    }}
    filterDropdownBackdrop?.classList.remove("open");
  }}

  function positionDropdownPanel(anchorBtn) {{
    if (!filterDropdownPanel || !anchorBtn) return;
    filterDropdownPanel.hidden = false;
    const rect = anchorBtn.getBoundingClientRect();
    const panelWidth = filterDropdownPanel.offsetWidth;
    const panelHeight = filterDropdownPanel.offsetHeight;
    let left = rect.left;
    let top = rect.bottom + 6;
    left = Math.max(8, Math.min(left, window.innerWidth - panelWidth - 8));
    if (top + panelHeight > window.innerHeight - 8) {{
      top = Math.max(8, rect.top - panelHeight - 6);
    }}
    filterDropdownPanel.style.left = left + "px";
    filterDropdownPanel.style.top = top + "px";
  }}

  function openDropdown(key) {{
    closeAllDropdowns();
    if (typeof closeSortMenu === "function") closeSortMenu();
    const wrap = pillWraps[key];
    if (!wrap || !filterDropdownPanel) return;
    const btn = wrap.querySelector(".sb-filter-pill");
    if (!btn) return;

    openDropdownKey = key;
    wrap.classList.add("is-open");
    btn.setAttribute("aria-expanded", "true");
    buildCheckboxList(filterDropdownPanel, key);
    const def = FILTER_DEFS.find(function(d) {{ return d.key === key; }});
    filterDropdownPanel.setAttribute("aria-label", (def ? def.label : "Filter") + " options");
    positionDropdownPanel(btn);
    filterDropdownBackdrop?.classList.add("open");
  }}

  function openFilterSheet() {{
    buildFilterSheet();
    filterSheet?.classList.add("open");
    filterSheetBackdrop?.classList.add("open");
    filterMasterBtn?.setAttribute("aria-expanded", "true");
    closeAllDropdowns();
    if (typeof closeSortMenu === "function") closeSortMenu();
  }}

  function closeFilterSheet() {{
    filterSheet?.classList.remove("open");
    filterSheetBackdrop?.classList.remove("open");
    updateFilterToggleLabel();
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
    if (isDesktopSidebar()) {{
      buildFilterSidebar();
    }}
    if (openDropdownKey && filterDropdownPanel && !filterDropdownPanel.hidden) {{
      const wrap = pillWraps[openDropdownKey];
      const btn = wrap?.querySelector(".sb-filter-pill");
      buildCheckboxList(filterDropdownPanel, openDropdownKey);
      if (btn) positionDropdownPanel(btn);
    }}
  }}

  function buildFilterBar() {{
    filterBarInner.innerHTML = "";
    FILTER_DEFS.forEach(function(def) {{
      const wrap = document.createElement("div");
      wrap.className = "sb-filter-pill-wrap";
      wrap.dataset.filterKey = def.key;

      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "sb-filter-pill";
      btn.setAttribute("aria-expanded", "false");
      btn.innerHTML = def.label + ' <span class="sb-filter-pill-chevron" aria-hidden="true">▾</span>';

      const badge = document.createElement("span");
      badge.className = "sb-filter-pill-badge";
      badge.hidden = true;
      btn.appendChild(badge);

      btn.addEventListener("click", function(e) {{
        e.stopPropagation();
        if (wrap.classList.contains("is-open")) closeAllDropdowns();
        else openDropdown(def.key);
      }});

      wrap.appendChild(btn);
      filterBarInner.appendChild(wrap);
      pillWraps[def.key] = wrap;
    }});
  }}

  function buildFilterSheet() {{
    if (!filterSheetBody) return;
    filterSheetBody.innerHTML = "";
    FILTER_DEFS.forEach(function(def) {{
      const group = document.createElement("div");
      group.className = "sb-filter-sheet-group";
      const title = document.createElement("div");
      title.className = "sb-filter-sheet-group-title";
      title.textContent = def.label;
      const body = document.createElement("div");
      buildCheckboxList(body, def.key);
      group.appendChild(title);
      group.appendChild(body);
      filterSheetBody.appendChild(group);
    }});
  }}

  function optionCountForKey(key, value) {{
    let n = 0;
    cards.forEach(function(card) {{
      if (!cardMatchesSelected(card, key)) return;
      if ((card.dataset[key] || "") === value) n += 1;
    }});
    return n;
  }}

  function buildFilterSidebar() {{
    if (!filterSidebarBody) return;
    const openState = {{}};
    filterSidebarBody.querySelectorAll("details.sb-filter-acc").forEach(function(d) {{
      openState[d.dataset.filterKey || ""] = d.open;
    }});
    filterSidebarBody.innerHTML = "";
    FILTER_DEFS.forEach(function(def) {{
      const details = document.createElement("details");
      details.className = "sb-filter-acc";
      details.dataset.filterKey = def.key;
      details.open = openState[def.key] !== false;
      const summary = document.createElement("summary");
      summary.textContent = def.label;
      const body = document.createElement("div");
      body.className = "sb-filter-acc-body";
      const options = getSmartOptionsForKey(def.key);
      if (!options.length) {{
        const empty = document.createElement("div");
        empty.className = "sb-filter-dropdown-empty";
        empty.textContent = "No options match.";
        body.appendChild(empty);
      }} else {{
        options.forEach(function(opt) {{
          const label = document.createElement("label");
          label.className = "sb-filter-check";
          const left = document.createElement("span");
          left.style.display = "inline-flex";
          left.style.alignItems = "center";
          left.style.gap = "10px";
          left.style.minWidth = "0";
          const cb = document.createElement("input");
          cb.type = "checkbox";
          cb.value = opt.value;
          cb.dataset.filterKey = def.key;
          cb.checked = selected[def.key].has(opt.value);
          cb.addEventListener("change", function() {{
            onFilterChange(def.key, opt.value, cb.checked);
          }});
          const span = document.createElement("span");
          span.textContent = opt.label;
          const count = document.createElement("span");
          count.className = "sb-filter-acc-count";
          count.textContent = String(optionCountForKey(def.key, opt.value));
          left.appendChild(cb);
          left.appendChild(span);
          label.appendChild(left);
          label.appendChild(count);
          body.appendChild(label);
        }});
      }}
      details.appendChild(summary);
      details.appendChild(body);
      filterSidebarBody.appendChild(details);
    }});
  }}

  function updateScrollButtonsFor(track, prevBtn, nextBtn, scrollWrap) {{
    if (!track || !prevBtn || !nextBtn) return;
    const maxScroll = track.scrollWidth - track.clientWidth;
    const show = maxScroll > 4;
    const hasPrev = show && track.scrollLeft > 4;
    const hasNext = show && track.scrollLeft < maxScroll - 4;
    prevBtn.classList.toggle("is-visible", hasPrev);
    nextBtn.classList.toggle("is-visible", hasNext);
    scrollWrap?.classList.toggle("has-scroll-prev", hasPrev);
    scrollWrap?.classList.toggle("has-scroll-next", hasNext);
  }}

  function updateScrollButtons() {{
    updateScrollButtonsFor(filterTrack, filterScrollPrev, filterScrollNext, filterBarScroll);
    updateScrollButtonsFor(tierTrack, tierScrollPrev, tierScrollNext, tierBarScroll);
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
    if (myItemsSummary) myItemsSummary.hidden = !myItemsView;
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

    // Dedupe live cards by item id in My Items (prefer first/highest already sorted later)
    const seenItemIds = new Set();

    cards.forEach(function(card) {{
      const r = card.dataset.retailer;
      const s = card.dataset.section;
      const c = card.dataset.category;
      const b = card.dataset.brand;
      const hay = (card.dataset.search || "").toLowerCase();
      const itemId = card.dataset.itemId || "";

      const matchR = selR.size === 0 || selR.has(r);
      const matchS = selS.size === 0 || selS.has(s);
      const matchC = selC.size === 0 || selC.has(c);
      const matchB = selB.size === 0 || selB.has(b);
      const matchQ = !q || hay.indexOf(q) !== -1;
      const matchT = cardMatchesTier(card);
      const matchStar = !myItemsView || (itemId && starredIds.has(itemId));

      if (myItemsView && itemId && seenItemIds.has(itemId)) {{
        card.style.display = "none";
        return;
      }}

      if (matchR && matchS && matchC && matchB && matchQ && matchT && matchStar) {{
        card.style.display = "";
        if (myItemsView && itemId) seenItemIds.add(itemId);
        fillLastSaleLines(card);
        shown.push(card);
      }} else {{
        card.style.display = "none";
        const box = card.querySelector(".sb-last-sale-lines");
        if (box) {{ box.hidden = true; box.innerHTML = ""; }}
      }}
    }});

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
        fillLastSaleLines(card);
        card.style.display = "";
        shown.push(card);
        seenItemIds.add(itemId);
      }});

      const totalStarred = starredIds.size;
      let onSaleCount = 0;
      starredIds.forEach(function(id) {{
        if (liveOnSaleIds.has(id)) onSaleCount += 1;
      }});
      if (myItemsSummary) {{
        myItemsSummary.hidden = false;
        myItemsSummary.textContent =
          onSaleCount + " of your " + totalStarred +
          (totalStarred === 1 ? " item is" : " items are") +
          " on sale right now.";
      }}
    }}

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
        resultsCount.textContent = shown.length + (shown.length === 1 ? " deal" : " deals");
      }}
    }}
  }}

  function clearAllFilters() {{
    selected.retailer.clear();
    selected.section.clear();
    selected.category.clear();
    selected.brand.clear();
    if (searchInput) searchInput.value = "";
    selectedTiers.clear();
    myItemsView = false;
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
  if (searchClear) {{
    searchClear.addEventListener("click", function() {{
      if (!searchInput) return;
      searchInput.value = "";
      searchInput.focus();
      lastTrackedSearch = "";
      clearTimeout(searchAnalyticsTimer);
      applyFiltersAndSort();
    }});
  }}
  if (sortSelect) {{
    sortSelect.addEventListener("change", applyFiltersAndSort);
  }}

  const sortPill = document.getElementById("sb-sort-pill");
  const sortMenu = document.getElementById("sb-sort-menu");

  function closeSortMenu() {{
    if (!sortMenu || !sortPill) return;
    sortMenu.hidden = true;
    sortPill.setAttribute("aria-expanded", "false");
  }}

  function syncSortMenuSelection() {{
    if (!sortMenu || !sortSelect) return;
    const current = sortSelect.value || "best";
    sortMenu.querySelectorAll(".sb-sort-option").forEach(function(btn) {{
      const on = btn.getAttribute("data-sort") === current;
      btn.classList.toggle("is-selected", on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
    }});
  }}

  function openSortMenu() {{
    if (!sortMenu || !sortPill) return;
    closeAllDropdowns();
    syncSortMenuSelection();
    sortMenu.hidden = false;
    sortPill.setAttribute("aria-expanded", "true");
  }}

  if (sortPill && sortMenu) {{
    sortPill.addEventListener("click", function(ev) {{
      ev.stopPropagation();
      if (sortMenu.hidden) openSortMenu();
      else closeSortMenu();
    }});
    sortMenu.addEventListener("click", function(ev) {{
      const opt = ev.target && ev.target.closest ? ev.target.closest(".sb-sort-option") : null;
      if (!opt) return;
      const value = opt.getAttribute("data-sort") || "best";
      if (sortSelect) {{
        sortSelect.value = value;
        sortSelect.dispatchEvent(new Event("change", {{ bubbles: true }}));
      }}
      syncSortMenuSelection();
      closeSortMenu();
    }});
    document.addEventListener("click", function(ev) {{
      if (!sortMenu.hidden && sortPill && !sortPill.contains(ev.target) && !sortMenu.contains(ev.target)) {{
        closeSortMenu();
      }}
    }});
  }}

  document.querySelectorAll(".sb-tier-chip[data-tier-filter]").forEach(function(el) {{
    el.addEventListener("click", function() {{
      const mode = el.getAttribute("data-tier-filter") || "all";
      setTierSelection(mode);
    }});
  }});

  buildFilterBar();
  buildFilterSheet();
  buildFilterSidebar();
  updateBadges();
  updateFilterToggleLabel();
  syncStarButtons();
  applyFiltersAndSort();

  fetch("last_sale.json", {{ cache: "no-store" }})
    .then(function(r) {{ return r.ok ? r.json() : null; }})
    .then(function(data) {{
      if (data && data.items) lastSaleItems = data.items;
      if (myItemsView) applyFiltersAndSort();
    }})
    .catch(function() {{}});

  /* ============================
     WELCOME PREFS + BEEHIIV EMBED
     ============================ */
  const BEEHIIV_FORM_ID = "{beehiiv_form_id}";
  const BEEHIIV_PUBLICATION_ID = "{beehiiv_pub_id}";
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
      const matchR = selected.retailer.size === 0 || selected.retailer.has(r);
      const matchS = selected.section.size === 0 || selected.section.has(s);
      const matchC = selected.category.size === 0 || selected.category.has(c);
      const matchB = selected.brand.size === 0 || selected.brand.has(b);
      const matchQ = !q || hay.indexOf(q) !== -1;
      const matchT = cardMatchesTier(card);
      if (matchR && matchS && matchC && matchB && matchQ && matchT) n += 1;
    }});
    return n;
  }}

  function applyWelcomePrefs(prefs) {{
    selected.retailer.clear();
    selected.section.clear();
    selected.category.clear();
    selected.brand.clear();
    (prefs.retailers || []).forEach(function(v) {{ if (v) selected.retailer.add(v); }});
    (prefs.categories || []).forEach(function(v) {{ if (v) selected.category.add(v); }});
    syncAllCheckboxes();
    updateBadges();
    applyFiltersAndSort();
    refreshFilterOptionLists();
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

  function submitWelcomeEmail(email, utmSource) {{
    // Hidden-iframe POST to beehiiv's public subscribe endpoint (same family as
    // their embed). We can't read the cross-origin response; friend tests /
    // Audience list confirm delivery.
    if (!BEEHIIV_PUBLICATION_ID) {{
      return Promise.reject(new Error("missing publication id"));
    }}
    return new Promise(function(resolve, reject) {{
      const frame =
        document.getElementById("sb-welcome-signup-frame") ||
        document.getElementById("sb-my-items-notify-frame");
      if (!frame) {{
        reject(new Error("missing frame"));
        return;
      }}

      const form = document.createElement("form");
      form.method = "POST";
      form.target = frame.name;
      form.action = "https://subscribe-forms.beehiiv.com/" + encodeURIComponent(BEEHIIV_PUBLICATION_ID) + "/submissions";
      form.style.display = "none";

      function addField(name, value) {{
        const input = document.createElement("input");
        input.type = "hidden";
        input.name = name;
        input.value = value;
        form.appendChild(input);
      }}
      addField("email", email);
      addField("reactivate_existing", "true");
      addField("send_welcome_email", "true");
      addField("utm_source", utmSource || "snackbuddy_welcome");
      if (BEEHIIV_FORM_ID) addField("form_id", BEEHIIV_FORM_ID);

      let settled = false;
      function done(ok) {{
        if (settled) return;
        settled = true;
        try {{ form.remove(); }} catch (err) {{}}
        if (ok) resolve();
        else reject(new Error("subscribe failed"));
      }}

      frame.onload = function() {{ done(true); }};
      document.body.appendChild(form);
      try {{
        form.submit();
      }} catch (err) {{
        done(false);
        return;
      }}
      window.setTimeout(function() {{ done(true); }}, 1200);
    }});
  }}

  function initWelcomeFlow() {{
    const root = document.getElementById("sb-welcome");
    if (!root) return;

    const existing = loadWelcomePrefs();
    if (existing && existing.completed) {{
      applyWelcomePrefs(existing);
      return;
    }}

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
        if (draft.subscribed) {{
          setEmailStatus("You’re already on the list.");
          return;
        }}
        const email = welcomeEmail ? String(welcomeEmail.value || "").trim() : "";
        if (!isValidEmail(email)) {{
          setEmailStatus("Enter a valid email address.", true);
          if (welcomeEmail) welcomeEmail.focus();
          return;
        }}
        if (!BEEHIIV_PUBLICATION_ID) {{
          setEmailStatus("Signup is temporarily unavailable. Try the footer form.", true);
          return;
        }}
        if (welcomeEmailSubmit) welcomeEmailSubmit.disabled = true;
        setEmailStatus("Subscribing…");
        draft.email = email;
        submitWelcomeEmail(email, "snackbuddy_welcome").then(function() {{
          draft.subscribed = true;
          markEmailSubscribed();
          syncMyItemsNotifyVisibility();
          trackEvent("generate_lead", {{ method: "welcome_flow" }});
          setEmailStatus("You’re in — watch for a welcome email from SnackBuddy.");
          if (welcomeEmail) welcomeEmail.blur();
        }}).catch(function() {{
          setEmailStatus("Couldn’t subscribe. Try again or use the footer form.", true);
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

      root.hidden = false;
      root.setAttribute("aria-hidden", "false");
      root.classList.add("open");
      document.body.classList.add("sb-welcome-lock");
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
      root.classList.remove("open");
      document.body.classList.remove("sb-welcome-lock");
      window.setTimeout(function() {{
        root.hidden = true;
        root.setAttribute("aria-hidden", "true");
      }}, 320);
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

    openWelcome();
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
      if (hasEmailSubscription()) {{
        setNotifyStatus("You’re already on the list.");
        syncMyItemsNotifyVisibility();
        return;
      }}
      const email = emailInput ? String(emailInput.value || "").trim() : "";
      if (!isValidEmail(email)) {{
        setNotifyStatus("Enter a valid email address.", true);
        if (emailInput) emailInput.focus();
        return;
      }}
      if (!BEEHIIV_PUBLICATION_ID) {{
        setNotifyStatus("Signup is temporarily unavailable. Try the footer form.", true);
        return;
      }}
      if (submitBtn) submitBtn.disabled = true;
      setNotifyStatus("Subscribing…");
      submitWelcomeEmail(email, "my_items_notify").then(function() {{
        markEmailSubscribed();
        trackEvent("generate_lead", {{ method: "my_items_notify" }});
        setNotifyStatus("You’re in — we’ll email when the good stuff drops.");
        if (emailInput) emailInput.blur();
        window.setTimeout(syncMyItemsNotifyVisibility, 1200);
      }}).catch(function() {{
        setNotifyStatus("Couldn’t subscribe. Try again or use the footer form.", true);
        if (submitBtn) submitBtn.disabled = false;
      }});
    }});
  }}

  initWelcomeFlow();
  wireMyItemsNotifySignup();

  filterMasterBtn?.addEventListener("click", function(e) {{
    e.stopPropagation();
    if (isDesktopSidebar()) {{
      dealsShell?.classList.toggle("is-filters-collapsed");
      updateFilterToggleLabel();
      return;
    }}
    if (filterSheet?.classList.contains("open")) closeFilterSheet();
    else openFilterSheet();
  }});

  filterSidebarClear?.addEventListener("click", clearAllFilters);
  filterSheetClose?.addEventListener("click", closeFilterSheet);
  filterSheetDone?.addEventListener("click", closeFilterSheet);
  filterSheetClear?.addEventListener("click", clearAllFilters);
  filterSheetBackdrop?.addEventListener("click", closeFilterSheet);
  filterDropdownBackdrop?.addEventListener("click", closeAllDropdowns);

  filterScrollPrev?.addEventListener("click", function() {{
    filterTrack.scrollBy({{ left: -180, behavior: "smooth" }});
  }});
  filterScrollNext?.addEventListener("click", function() {{
    filterTrack.scrollBy({{ left: 180, behavior: "smooth" }});
  }});
  tierScrollPrev?.addEventListener("click", function() {{
    tierTrack.scrollBy({{ left: -180, behavior: "smooth" }});
  }});
  tierScrollNext?.addEventListener("click", function() {{
    tierTrack.scrollBy({{ left: 180, behavior: "smooth" }});
  }});
  filterTrack?.addEventListener("scroll", function() {{
    updateScrollButtons();
    if (openDropdownKey) {{
      const btn = pillWraps[openDropdownKey]?.querySelector(".sb-filter-pill");
      if (btn && filterDropdownPanel && !filterDropdownPanel.hidden) positionDropdownPanel(btn);
    }}
  }});
  tierTrack?.addEventListener("scroll", updateScrollButtons);
  window.addEventListener("resize", function() {{
    updateScrollButtons();
    updateFilterToggleLabel();
    if (isDesktopSidebar()) buildFilterSidebar();
    if (openDropdownKey) {{
      const btn = pillWraps[openDropdownKey]?.querySelector(".sb-filter-pill");
      if (btn && filterDropdownPanel && !filterDropdownPanel.hidden) positionDropdownPanel(btn);
    }}
  }});
  updateScrollButtons();
  updateFilterToggleLabel();

  document.addEventListener("keydown", function(ev) {{
    if (ev.key === "Escape") {{
      closeAllDropdowns();
      closeSortMenu();
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
    form_id = BEEHIIV_FORM_ID
    if form_id:
        print("BEEHIIV FORM:", form_id[:8] + "…" if len(form_id) > 8 else form_id)
    else:
        print("BEEHIIV FORM: (not set)")
    pub = BEEHIIV_PUBLICATION_ID
    if pub:
        print("BEEHIIV PUB:", pub[:8] + "…" if len(pub) > 8 else pub)

if __name__ == "__main__":
    main()

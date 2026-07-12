import json
import html
import random
from pathlib import Path
from datetime import datetime


ROOT = Path(__file__).parent
JSON_PATH = ROOT / "deals_today.json"
DOCS_DIR = ROOT / "docs"
OUTPUT_PATH = DOCS_DIR / "index.html"

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


def get_last_updated_text(deals):
    # Use the newest verified_at if available; otherwise now()
    times = [t for t in (parse_verified_at(d) for d in deals) if t is not None]
    if times:
        latest = max(times)
    else:
        latest = datetime.now()

    # Example: December 07, 2025 at 11:20 AM
    return latest.strftime("%B %d, %Y at %I:%M %p")


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
    if not isinstance(raw, str):
        return "Check store availability", "availability check"
    val = raw.strip().lower()
    if val == "in_store":
        return "In-store", "availability in-store"
    if val == "both":
        return "In-store & online", "availability both"
    if val == "online_only":
        return "Online only", "availability online-only"
    # Unknown / blank / other → generic helpful message
    return "Check store availability", "availability check"


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


def get_tier_name(percent_off):
    """
    Get tier name from percent_off for filtering.
    Returns: "diamond", "fire", "strong", or "sale"
    """
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
    """Per-card randomized ✦ sparkles — stable layout for a given deal, varied across cards."""
    rng = random.Random(seed_key)
    count = rng.randint(5, 9)
    twinkle_anims = (
        "diamond-twinkle-a",
        "diamond-twinkle-b",
        "diamond-twinkle-c",
        "diamond-twinkle-d",
    )
    timing_fns = ("linear",)

    def sparkle_size(base: int) -> tuple[int, int]:
        """Square canvas keeps the sparkle SVG symmetric."""
        return base, base

    stars = []
    for _ in range(count):
        width, height = sparkle_size(rng.randint(14, 30))

        top = rng.uniform(5, 86)
        if rng.random() < 0.5:
            horizontal = f"left:{rng.uniform(3, 84):.1f}%;"
        else:
            horizontal = f"right:{rng.uniform(3, 84):.1f}%;left:auto;"

        anim = rng.choice(twinkle_anims)
        duration = rng.uniform(5.0, 11.5)
        delay = rng.uniform(0, 9.5)
        peak = rng.uniform(0.75, 1.2)

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
    - ≥25% → 💎 Diamond (badge-elite)
    - 20–24.99% → 🔥 Fire (badge-strong)
    - 10–19.99% → 💪 Strong (badge-protein)
    - 0–9.99% → 🏷️ On Sale (badge-everyday)
    """
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


def build_card_html(deal):
    # Use product_name directly from the deal (from CSV, not combined)
    # Remove pack size indicators since we have a pack pill overlay
    product_name = deal.get("product_name", "")
    product_name_cleaned = remove_pack_size_from_name(product_name)
    name = html.escape(product_name_cleaned)
    retailer = html.escape(deal.get("retailer", ""))
    category = html.escape(deal.get("category", ""))
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

    section_attr = html.escape(section_raw)
    category_attr = html.escape(category_raw)
    retailer_attr = html.escape(retailer_raw)
    brand_attr = html.escape(brand_raw)

    # Retailer specific styling
    pill_class, button_class = retailer_classes(deal.get("retailer", ""))

    # Availability
    availability_text, availability_class = normalise_availability(
        deal.get("availability", "")
    )

    # Badge
    badge_label, badge_class = get_badge(deal)
    
    # Tier for filtering
    tier_name = get_tier_name(percent_off)
    tier_class = f"card-tier-{tier_name}"

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

    old_price_html = ""
    if old_price is not None:
        try:
            old_price_val = float(old_price)
            if old_price_val > new_price_val:
                old_price_html = (
                    f"<span class='old-price'>${old_price_val:.2f}</span>"
                )
        except Exception:
            old_price_html = ""

    availability_html = ""
    if availability_text:
        availability_html = f"<div class='{availability_class}'>{availability_text}</div>"

    streak_html = ""
    if streak_text:
        streak_html = f"<div class='streak'>{streak_text}</div>"

    badge_html = ""
    if badge_label and badge_class:
        if tier_name == "fire":
            import hashlib
            flicker_seed = f"{retailer_raw}|{name}|{new_price_val}"
            flicker_delay = (
                int(hashlib.md5(flicker_seed.encode()).hexdigest()[:4], 16) % 80
            ) / 10.0
            badge_html = (
                f"<div class='{badge_class} badge-fire-pill'>"
                f"<span class='fire-pill-flames' style='animation-delay:{flicker_delay:.1f}s' "
                f"aria-hidden='true'>{fire_pill_flames_svg()}</span>"
                f"<span class='fire-pill-label'>🔥 Fire</span></div>"
            )
        else:
            badge_html = f"<div class='{badge_class}'>{badge_label}</div>"

    diamond_sparkles_html = ""
    if tier_name == "diamond":
        sparkle_seed = f"{retailer_raw}|{name}|{new_price_val}|{deal_count}"
        diamond_sparkles_html = build_diamond_sparkles_html(sparkle_seed)

    category_tag_html = ""
    if category:
        category_tag_html = f'<div class="card-category-tag">{category}</div>'

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
    <div class="card {tier_class}"
         data-section="{section_attr}"
         data-category="{category_attr}"
         data-retailer="{retailer_attr}"
         data-brand="{brand_attr}"
         data-price="{new_price_val}"
         data-percent="{float(percent_off) if percent_off is not None else 0.0}"
         data-deal-count="{deal_count}"
         data-tier="{tier_name}">
        {diamond_sparkles_html}
        <div class="card-image-wrap">
            {image_carousel_html}
            {category_tag_html}
            {pack_pill_html}
            {badge_html}
        </div>
        <div class="card-content">
            <div class="card-pricing">
                {old_price_html}
                <span class="new-price">${new_price_val:.2f}</span>
                <span class="percent-off">{percent_off:.0f}% OFF</span>
            </div>
            <div class="card-title">{name}</div>
            {flavor_html}
            <div class="card-footer">
                {availability_html}
                {streak_html}
            </div>
            <a class="{button_class}" href="{retailer_url}" target="_blank" rel="noopener noreferrer">
                View on {retailer_cta_label(deal.get("retailer", ""))}.com
            </a>
        </div>
    </div>
    """


def build_page_html(deals):
    # Group deals into deal families
    grouped_deals = group_deals(deals)
    # Calculate total number of individual deals (flavors) across all groups
    total_deals = sum(
        len(g.get("flavor_data", [])) if g.get("flavor_data") else 1
        for g in grouped_deals
    )
    last_updated = get_last_updated_text(deals)

    # Sort: tier (diamond first, then fire, then strong, then sale) → best savings → price
    def sort_key(d):
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
    cards_html = "\n".join(build_card_html(d) for d in deals_sorted)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8" />
    <title>SnackBuddy Daily Deals</title>
    <meta name="viewport" content="width=device-width, initial-scale=1" />
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
            max-width: 1100px;
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
            max-width: 1100px;
            margin: 0 auto;
            padding: 10px 16px;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }}

        .sb-header-left {{
            display: flex;
            align-items: center;
            gap: 8px;
        }}

        /* Logo image (replaces placeholder box) */
        .sb-logo {{
            width: 32px;
            height: 32px;
            border-radius: 8px;
            object-fit: contain;
            display: block;
        }}

        .sb-header-title {{
            font-weight: 600;
            font-size: 18px;
            color: var(--text-main);
        }}

        .sb-menu-button {{
            border: none;
            background: transparent;
            font-size: 20px;
            cursor: pointer;
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
  max-width: 1100px;
  margin: 10px auto 12px;
  padding: 0 16px;
  position: relative;
  z-index: 30;
}}

.sb-filter-bar {{
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}}

.sb-filter-master {{
  position: relative;
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 44px;
  height: 44px;
  padding: 0;
  border: 1px solid #111827;
  border-radius: 999px;
  background: #ffffff;
  cursor: pointer;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.06);
}}

.sb-filter-master:hover {{
  filter: brightness(0.98);
}}

.sb-filter-master-icon {{
  width: 22px;
  height: 22px;
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
  z-index: 3;
  flex-shrink: 0;
  width: 32px;
  height: 32px;
  border: 1px solid var(--border-subtle);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.96);
  color: var(--text-main);
  font-size: 20px;
  line-height: 1;
  cursor: pointer;
  display: none;
  align-items: center;
  justify-content: center;
  box-shadow: 0 1px 4px rgba(15, 23, 42, 0.12);
}}

.sb-filter-scroll-btn.is-visible {{
  display: inline-flex;
}}

.sb-filter-scroll-prev {{
  left: 0;
}}

.sb-filter-scroll-next {{
  right: 0;
}}

.sb-filter-bar-scroll {{
  position: relative;
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
}}

.sb-filter-bar-scroll.has-scroll-prev .sb-filter-bar-track {{
  padding-left: 38px;
}}

.sb-filter-bar-scroll.has-scroll-next .sb-filter-bar-track {{
  padding-right: 38px;
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
  gap: 8px;
  padding: 4px 2px;
  min-width: min-content;
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
  max-width: 1100px;
  margin: 0 auto;
  padding: 0 16px;
}}

.sb-deals-main {{
  min-width: 0;
}}

        /* ============================ */
        /* SECTION 1B: HERO BANNER      */
        /* ============================ */

        .sb-hero {{
            margin-top: 8px;
        }}

        .sb-hero-bg {{
            max-width: 1100px;
            margin: 0 auto;
            border-radius: 18px;
            padding: 18px 18px 14px;
            position: relative;
            overflow: hidden;
            background: #ffffff;
            color: #111827;
            box-shadow: 0 10px 24px rgba(15, 23, 42, 0.12);
            border: 1px solid #e5e7eb;
        }}

        /* wallpaper brand texture */
        .sb-hero-pattern {{
            position: absolute;
            inset: 0;
            opacity: 0.16;
            background-image: url("assets/hero-wallpaper.png");
            background-repeat: repeat;
            background-size: 360px auto;
            background-position: top left;
            filter: brightness(1.18) contrast(0.85);
            pointer-events: none;
        }}

        /* soft white wash so text stays high-contrast */
        .sb-hero-bg::after {{
            content: "";
            position: absolute;
            inset: 0;
            background: rgba(255, 255, 255, 0.44);
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
            max-width: 1100px;
            margin: 0 auto;
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
            gap: 14px;
        }}

        @media (max-width: 640px) {{
            .card-grid {{
                grid-template-columns: 1fr;
                gap: 12px;
            }}
        }}

        .card {{
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
            background-color: #eff6ff;
            border-color: #93c5fd;
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
            animation: diamond-twinkle-a 4s linear infinite;
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
            filter: drop-shadow(0 0 3px rgba(37, 99, 235, 0.75));
        }}

        @keyframes diamond-twinkle-a {{
            0%, 100% {{
                opacity: 0;
                transform: scale(0.05);
            }}
            7% {{
                opacity: 0.45;
                transform: scale(calc(var(--sparkle-peak, 1.15) * 0.45));
            }}
            11% {{
                opacity: 1;
                transform: scale(var(--sparkle-peak, 1.15));
            }}
            15% {{
                opacity: 0.45;
                transform: scale(calc(var(--sparkle-peak, 1.15) * 0.45));
            }}
            19% {{
                opacity: 0;
                transform: scale(0.05);
            }}
        }}

        @keyframes diamond-twinkle-b {{
            0%, 100% {{
                opacity: 0;
                transform: scale(0.05);
            }}
            6% {{
                opacity: 0.45;
                transform: scale(calc(var(--sparkle-peak, 1.1) * 0.45));
            }}
            10% {{
                opacity: 1;
                transform: scale(var(--sparkle-peak, 1.1));
            }}
            14% {{
                opacity: 0.45;
                transform: scale(calc(var(--sparkle-peak, 1.1) * 0.45));
            }}
            18% {{
                opacity: 0;
                transform: scale(0.05);
            }}
            58% {{
                opacity: 0;
                transform: scale(0.05);
            }}
            64% {{
                opacity: 0.45;
                transform: scale(calc(var(--sparkle-peak, 1.1) * 0.45));
            }}
            68% {{
                opacity: 1;
                transform: scale(calc(var(--sparkle-peak, 1.1) * 0.92));
            }}
            72% {{
                opacity: 0.45;
                transform: scale(calc(var(--sparkle-peak, 1.1) * 0.45));
            }}
            76% {{
                opacity: 0;
                transform: scale(0.05);
            }}
        }}

        @keyframes diamond-twinkle-c {{
            0%, 100% {{
                opacity: 0;
                transform: scale(0.05);
            }}
            38% {{
                opacity: 0;
                transform: scale(0.05);
            }}
            44% {{
                opacity: 0.45;
                transform: scale(calc(var(--sparkle-peak, 1.2) * 0.45));
            }}
            48% {{
                opacity: 1;
                transform: scale(var(--sparkle-peak, 1.2));
            }}
            52% {{
                opacity: 0.45;
                transform: scale(calc(var(--sparkle-peak, 1.2) * 0.45));
            }}
            56% {{
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
                opacity: 0.4;
                transform: scale(calc(var(--sparkle-peak, 1) * 0.4));
            }}
            7% {{
                opacity: 0.95;
                transform: scale(calc(var(--sparkle-peak, 1) * 0.88));
            }}
            10% {{
                opacity: 0;
                transform: scale(0.05);
            }}
            30% {{
                opacity: 0;
                transform: scale(0.05);
            }}
            33% {{
                opacity: 0.4;
                transform: scale(calc(var(--sparkle-peak, 1) * 0.4));
            }}
            36% {{
                opacity: 0.95;
                transform: scale(calc(var(--sparkle-peak, 1) * 0.88));
            }}
            39% {{
                opacity: 0;
                transform: scale(0.05);
            }}
            68% {{
                opacity: 0;
                transform: scale(0.05);
            }}
            71% {{
                opacity: 0.4;
                transform: scale(calc(var(--sparkle-peak, 1) * 0.4));
            }}
            74% {{
                opacity: 0.95;
                transform: scale(calc(var(--sparkle-peak, 1) * 0.88));
            }}
            77% {{
                opacity: 0;
                transform: scale(0.05);
            }}
        }}

        @media (prefers-reduced-motion: reduce) {{
            .diamond-sparkles {{
                display: none;
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

        .card-category-tag {{
            position: absolute;
            top: 6px;
            right: 8px;
            font-size: 11px;
            color: var(--text-muted);
            z-index: 12;
            max-width: 48%;
            text-align: right;
            line-height: 1.25;
            pointer-events: none;
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
            top: 6px;
            left: 8px;
            z-index: 12;
            border-radius: var(--radius-pill);
            padding: 2px 8px;
            font-size: 11px;
            font-weight: 600;
            display: inline-flex;
            align-items: center;
            gap: 4px;
            background-color: #ffffff;
            border: 1px solid #e5e7eb;
            box-shadow: 0 1px 2px rgba(15, 23, 42, 0.08);
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
            height: 13px;
            pointer-events: none;
            z-index: 1;
            opacity: 0;
            transform: scaleY(0.15) translateY(3px);
            transform-origin: center bottom;
            animation: fire-pill-flames-rise 10s linear infinite;
        }}

        .fire-pill-flames-svg {{
            display: block;
            width: 100%;
            height: 100%;
        }}

        .fire-pill-spark {{
            opacity: 0;
            transform-origin: center center;
            animation: fire-pill-spark-pop 10s linear infinite;
            animation-delay: inherit;
        }}

        @keyframes fire-pill-flames-rise {{
            0%, 78%, 100% {{
                opacity: 0;
                transform: scaleY(0.12) translateY(4px);
            }}
            82% {{
                opacity: 0.65;
                transform: scaleY(0.55) translateY(2px);
            }}
            86% {{
                opacity: 1;
                transform: scaleY(0.95) translateY(0);
            }}
            90% {{
                opacity: 1;
                transform: scaleY(1.08) translateY(-1px);
            }}
            94% {{
                opacity: 0.8;
                transform: scaleY(0.72) translateY(1px);
            }}
            98% {{
                opacity: 0;
                transform: scaleY(0.1) translateY(4px);
            }}
        }}

        @keyframes fire-pill-spark-pop {{
            0%, 84%, 100% {{
                opacity: 0;
                transform: translateY(6px) scale(0.4);
            }}
            88% {{
                opacity: 0.9;
                transform: translateY(0) scale(1);
            }}
            92% {{
                opacity: 0.55;
                transform: translateY(-3px) scale(0.85);
            }}
            96% {{
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
            color: #1e40af;
            border-color: #93c5fd;
        }}

        .badge-protein {{
            color: #854d0e;
            border-color: #facc15;
        }}

        .badge-everyday {{
            color: #6b7280;
            border-color: #d1d5db;
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
            max-width: 1100px;
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

        @media (max-width: 640px) {{
            .page {{
                padding-top: 8px;
            }}
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
    <a href="#subscribe" class="sb-nav-link">Get Daily Deal Emails</a>

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
                    <div class="sb-hero-kicker">
                        SnackBuddy • Last updated: {last_updated} (local time)
                    </div>
                    <h1 class="sb-hero-title">Don&apos;t overpay for the snacks you already buy.</h1>
                </div>
            </div>
        </section>

        <section class="sb-filter-bar-section" aria-label="Deal filters">
            <div class="sb-filter-bar">
                <button type="button" class="sb-filter-master" id="sb-filter-master" aria-label="All filters" aria-expanded="false">
                    <svg class="sb-filter-master-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                        <path fill="currentColor" d="M4.5 5.75c0-.69.56-1.25 1.25-1.25h12.5c.69 0 1.25.56 1.25 1.25v.51c0 .24-.07.47-.2.67l-5.2 7.28v5.04c0 .41-.25.78-.63.92l-2.45 1.02c-.77.32-1.63-.24-1.63-1.08v-6.9L4.7 6.93c-.13-.2-.2-.43-.2-.67v-.51z"/>
                    </svg>
                    <span class="sb-filter-master-badge" id="sb-filter-master-count" hidden>0</span>
                </button>
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

        <div class="sb-deals-shell">
        <div class="sb-deals-main">
        <section class="card-grid" id="deals-list">
            {cards_html}
        </section>

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
                    <strong>track the snacks and drinks people already buy and call out real price drops</strong> —
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
                    When something drops, it shows up here so more people can take advantage of it — without having to constantly
                    check themselves.
                </p>
                <p class="sb-info-text" style="margin-top: 8px;">
                    Don’t see a product or retailer you want tracked yet? Let me know — the list is expanding over time.
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
                    Not everything that calls itself healthy earns a spot here. Every product is manually reviewed — we&apos;re looking for real protein content relative to calories, low added sugar, and meaningful fiber where it applies.
                </p>
                <p class="sb-info-text" style="margin-top: 8px;">
                    We&apos;re not here to be perfect. SnackBuddy isn&apos;t about finding the most aggressive diet food on the shelf. It&apos;s about finding things that are genuinely better than the default — snacks people actually want to eat, at a price worth buying them.
                </p>
                <p class="sb-info-text" style="margin-top: 8px;">
                    Don&apos;t see something you think belongs here? Let us know — the list is always growing.
                </p>
            </div>
        </section>

            <div class="sb-info-card" id="subscribe">
                <h2 class="sb-info-title">Get Daily Deal Emails</h2>
                <p class="sb-info-text">
                    Want a quick digest of the best finds? Drop your email below.
                </p>

                <div class="sb-subscribe-row">
    <a class="sb-subscribe-btn"
       href="https://forms.gle/YatCfNYrLdALVq1N8"
       target="_blank"
       rel="noopener noreferrer">
        Get Daily Deal Emails
    </a>

    <a class="sb-subscribe-alt"
       href="mailto:savewithsnackbuddy@gmail.com?subject=SnackBuddy%20Daily%20Deals">
        Or email us: savewithsnackbuddy@gmail.com
    </a>
</div>

<div class="sb-note">
    You’ll get a quick daily digest when the best deals are worth grabbing. No spam—ever.
</div>

            </div>
        </section>
    </main>

    <!-- Tiny JS for slide-out menu -->
<script>
document.addEventListener("DOMContentLoaded", function () {{
  const grid = document.getElementById("deals-list");
  const cards = Array.from(document.querySelectorAll(".card"));

  const filterBarInner = document.getElementById("sb-filter-bar-inner");
  const filterSheetBody = document.getElementById("sb-filter-sheet-body");
  const filterMasterBtn = document.getElementById("sb-filter-master");
  const filterMasterCount = document.getElementById("sb-filter-master-count");
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

  const pillWraps = {{}};
  let openDropdownKey = null;

  function cardMatchesSelected(card, excludeKey) {{
    const r = card.dataset.retailer;
    const s = card.dataset.section;
    const c = card.dataset.category;
    const b = card.dataset.brand;

    if (excludeKey !== "retailer" && selected.retailer.size > 0 && !selected.retailer.has(r)) return false;
    if (excludeKey !== "section" && selected.section.size > 0 && !selected.section.has(s)) return false;
    if (excludeKey !== "category" && selected.category.size > 0 && !selected.category.has(c)) return false;
    if (excludeKey !== "brand" && selected.brand.size > 0 && !selected.brand.has(b)) return false;
    return true;
  }}

  function getSmartOptionsForKey(key) {{
    const matching = cards.filter(function(card) {{ return cardMatchesSelected(card, key); }});

    return uniq(matching.map(function(card) {{ return card.dataset[key]; }})).map(function(v) {{
      return {{ value: v, label: titleize(v) }};
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
    filterSheet?.classList.add("open");
    filterSheetBackdrop?.classList.add("open");
    filterMasterBtn?.setAttribute("aria-expanded", "true");
    closeAllDropdowns();
  }}

  function closeFilterSheet() {{
    filterSheet?.classList.remove("open");
    filterSheetBackdrop?.classList.remove("open");
    filterMasterBtn?.setAttribute("aria-expanded", "false");
  }}

  function onFilterChange(key, value, checked) {{
    if (checked) selected[key].add(value);
    else selected[key].delete(value);
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

  function updateScrollButtons() {{
    if (!filterTrack || !filterScrollPrev || !filterScrollNext) return;
    const maxScroll = filterTrack.scrollWidth - filterTrack.clientWidth;
    const show = maxScroll > 4;
    const hasPrev = show && filterTrack.scrollLeft > 4;
    const hasNext = show && filterTrack.scrollLeft < maxScroll - 4;
    filterScrollPrev.classList.toggle("is-visible", hasPrev);
    filterScrollNext.classList.toggle("is-visible", hasNext);
    filterBarScroll?.classList.toggle("has-scroll-prev", hasPrev);
    filterBarScroll?.classList.toggle("has-scroll-next", hasNext);
  }}

  function applyFiltersAndSort() {{
    pruneInvalidSelections();
    const selR = selected.retailer;
    const selS = selected.section;
    const selC = selected.category;
    const selB = selected.brand;
    let shown = [];

    cards.forEach(function(card) {{
      const r = card.dataset.retailer;
      const s = card.dataset.section;
      const c = card.dataset.category;
      const b = card.dataset.brand;

      const matchR = selR.size === 0 || selR.has(r);
      const matchS = selS.size === 0 || selS.has(s);
      const matchC = selC.size === 0 || selC.has(c);
      const matchB = selB.size === 0 || selB.has(b);

      if (matchR && matchS && matchC && matchB) {{
        card.style.display = "";
        shown.push(card);
      }} else {{
        card.style.display = "none";
      }}
    }});

    shown.sort(function(a, b) {{
      const aPrice = parseFloat(a.dataset.price || "0");
      const bPrice = parseFloat(b.dataset.price || "0");
      const aPct = parseFloat(a.dataset.percent || "0");
      const bPct = parseFloat(b.dataset.percent || "0");
      const aTier = a.dataset.tier || "sale";
      const bTier = b.dataset.tier || "sale";

      function getTierPriority(tier) {{
        if (tier === "diamond") return 4;
        if (tier === "fire") return 3;
        if (tier === "strong") return 2;
        return 1;
      }}

      const aTierPriority = getTierPriority(aTier);
      const bTierPriority = getTierPriority(bTier);
      if (aTierPriority !== bTierPriority) return bTierPriority - aTierPriority;
      if (aPct !== bPct) return bPct - aPct;
      return aPrice - bPrice;
    }});

    shown.forEach(function(card) {{ grid.appendChild(card); }});
  }}

  function clearAllFilters() {{
    selected.retailer.clear();
    selected.section.clear();
    selected.category.clear();
    selected.brand.clear();
    syncAllCheckboxes();
    updateBadges();
    applyFiltersAndSort();
    refreshFilterOptionLists();
  }}

  buildFilterBar();
  buildFilterSheet();
  updateBadges();
  applyFiltersAndSort();

  filterMasterBtn?.addEventListener("click", function() {{
    if (filterSheet?.classList.contains("open")) closeFilterSheet();
    else openFilterSheet();
  }});
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
  filterTrack?.addEventListener("scroll", function() {{
    updateScrollButtons();
    if (openDropdownKey) {{
      const btn = pillWraps[openDropdownKey]?.querySelector(".sb-filter-pill");
      if (btn && filterDropdownPanel && !filterDropdownPanel.hidden) positionDropdownPanel(btn);
    }}
  }});
  window.addEventListener("resize", function() {{
    updateScrollButtons();
    if (openDropdownKey) {{
      const btn = pillWraps[openDropdownKey]?.querySelector(".sb-filter-pill");
      if (btn && filterDropdownPanel && !filterDropdownPanel.hidden) positionDropdownPanel(btn);
    }}
  }});
  updateScrollButtons();

  document.addEventListener("keydown", function(ev) {{
    if (ev.key === "Escape") {{
      closeAllDropdowns();
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
    page_html = build_page_html(deals)

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(page_html, encoding="utf-8")
    print("SCRIPT FILE:", Path(__file__).resolve())
    print("OUTPUT PATH:", OUTPUT_PATH.resolve())
    print("WROTE:", OUTPUT_PATH.resolve())
    print("DEALS:", len(deals))

if __name__ == "__main__":
    main()                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              